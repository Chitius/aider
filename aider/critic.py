import os
import re
from typing import Dict, List, Optional, Tuple, Union
import sys
sys.path.append("/home/admin/workspace/playground/modelscope-agent")

import json5
import logging
from modelscope_agent import Agent
from modelscope_agent.agent_env_util import AgentEnvMixin
from modelscope_agent.llm.base import BaseChatModel
from modelscope_agent.utils.base64_utils import encode_files_to_base64
from modelscope_agent.utils.logger import agent_logger as logger
from modelscope_agent.llm.utils.function_call_with_raw_prompt import detect_multi_tool
from modelscope_agent.utils.tokenization_utils import count_tokens
from modelscope_agent.utils.utils import check_and_limit_input_length
from modelscope_agent.schemas import CodeCell, Plan, Task, TaskResult
from modelscope_agent.utils.utils import parse_code

KNOWLEDGE_TEMPLATE_ZH = """

# 知识库

{ref_doc}

"""

PROMPT_TEMPLATE_ZH = """
{role_prompt}

# Task:
Based on the user's current task and the actions taken to achieve the task, determine whether the user's behavior is satisfactory, if not, provide suggestions to the user to help the user improve their work.

If the user is on his/her last task, you should consider the problem from a global perspective, \
consider whether the user's current series of behaviors are sufficient to satisfactorily achieve their optimal goals, and provide appropriate suggestions.

If the user skip, omit or elide any code content in his edition, you must suggest to the user not to skip the missing code content.

Output a json following the format:
```json
{{
    "pass": str = "OK" if you believe that the user's behavior has satisfactorily met the requirement, otherwise "HOLD",
    "suggestions": str = "Suggestions for users consisting of one to two sentences should be concise and have substance",
}}
```
"""

KNOWLEDGE_TEMPLATE_EN = """

# Knowledge Base

{ref_doc}

"""

PROMPT_TEMPLATE_EN = PROMPT_TEMPLATE_ZH

KNOWLEDGE_TEMPLATE = {'zh': KNOWLEDGE_TEMPLATE_ZH, 'en': KNOWLEDGE_TEMPLATE_EN}

PROMPT_TEMPLATE = {
    'zh': PROMPT_TEMPLATE_ZH,
    'en': PROMPT_TEMPLATE_EN,
}

PREFIX_PROMPT_TEMPLATE = {
    'zh': '，明白了请说“好的。”，不要说其他的。',
    'en': ', say "OK." if you understand, do not say anything else.'
}

SYSTEM_ANSWER_TEMPLATE = {
    'zh': '好的。',
    'en': 'OK.',
}

SPECIAL_PREFIX_TEMPLATE_ROLE = {
    'zh': '你正在扮演{role_name}',
    'en': 'You are playing as {role_name}',
}

SPECIAL_PREFIX_TEMPLATE_KNOWLEDGE = {
    'zh': '。请查看前面的知识库',
    'en': '. Please read the knowledge base at the beginning',
}

SPECIAL_PREFIX_TEMPLATE_FILE = {
    'zh': '[上传文件 "{file_names}"]',
    'en': '[Upload file "{file_names}"]',
}


class CriticAgent(Agent, AgentEnvMixin):

    def __init__(self,
                 llm: Optional[Union[Dict, BaseChatModel]] = None,
                 storage_path: Optional[str] = None,
                 name: Optional[str] = None,
                 description: Optional[str] = None,
                 instruction: Union[str, dict] = "",
                 do_log: bool = True,
                 **kwargs):
        Agent.__init__(self, [], llm, storage_path, name,
                       description, instruction, **kwargs)
        AgentEnvMixin.__init__(self, **kwargs)
        global logger
        if not do_log:
            for handler in logger.logger.handlers:
                if isinstance(handler, logging.StreamHandler):
                    logger.logger.removeHandler(handler)
    def _run(self,
             user_request,
             ref_doc: str = None,
             image_url: Optional[List[Union[str, Dict]]] = None,
             lang: str = 'zh',
             mid_messages: List[Dict] = None,
             curr_plan: Plan = None,
             **kwargs):

        chat_mode = kwargs.pop('chat_mode', False)
        self.system_prompt = ''
        self.query_prefix = ''
        self.query_prefix_dict = {'role': '', 'tool': '', 'knowledge': ''}
        append_files = kwargs.get('append_files', [])

        # concat knowledge
        if ref_doc:
            knowledge_limit = kwargs.get('knowledge_limit',
                                         os.getenv('KNOWLEDGE_LIMIT', 4000))
            ref_doc = check_and_limit_input_length(ref_doc, knowledge_limit)
            self.system_prompt += KNOWLEDGE_TEMPLATE[lang].format(
                ref_doc=ref_doc)
            self.query_prefix_dict[
                'knowledge'] = SPECIAL_PREFIX_TEMPLATE_KNOWLEDGE[lang]

        # concat instruction
        if isinstance(self.instruction, dict):
            self.role_name = self.instruction['name']
            self.query_prefix_dict['role'] = SPECIAL_PREFIX_TEMPLATE_ROLE[
                lang].format(role_name=self.role_name)
            self.system_prompt += PROMPT_TEMPLATE[lang].format(
                role_prompt=self._parse_role_config(self.instruction, lang))
        else:
            # string can not parser role name
            self.role_name = ''
            self.system_prompt += PROMPT_TEMPLATE[lang].format(
                role_prompt=self.instruction)

        self.query_prefix = ''
        self.query_prefix += self.query_prefix_dict['role']
        self.query_prefix += self.query_prefix_dict['tool']
        self.query_prefix += self.query_prefix_dict['knowledge']
        if self.query_prefix:
            self.query_prefix = '(' + self.query_prefix + ')'

        if len(append_files) > 0:
            file_names = ','.join(
                [os.path.basename(path) for path in append_files])
            self.query_prefix += SPECIAL_PREFIX_TEMPLATE_FILE[lang].format(
                file_names=file_names)

        # Concat the system as one round of dialogue
        messages = [{'role': 'system', 'content': self.system_prompt}]

        if mid_messages:
            if not isinstance(mid_messages, list):
                mid_messages = [mid_messages,]
            messages.extend(mid_messages)

        # concat the new messages
        messages.append({
            'role': 'user',
            'content': self.query_prefix + user_request
        })

        if image_url:
            self._parse_image_url(image_url, messages)

        planning_prompt = ''
        if self.llm.support_raw_prompt() and hasattr(self.llm,
                                                     'build_raw_prompt'):
            planning_prompt = self.llm.build_raw_prompt(messages)

        self.callback_manager.on_step_start()
        output = self.llm.chat(
            prompt=planning_prompt,
            stream=self.stream,
            messages=messages,
            callbacks=self.callback_manager,
            **kwargs)

        llm_result = ''
        for s in output:
            if isinstance(s, dict):
                llm_result = s
                break
            else:
                llm_result += s

        self.callback_manager.on_step_end()
        llm_result = llm_result.strip()
        if not llm_result.startswith("```json"):
            llm_result = "```json\n" + llm_result
        if not llm_result.endswith("```"):
            llm_result += "\n```"
        return llm_result
        
def extract_suggestion(llm_result: str) -> Tuple[str, bool]:
    output = parse_code(text = llm_result, lang = 'json')
    output_dict = json5.loads(output)
    while isinstance(output_dict, list):
        output_dict = output_dict[0]
    good_work = (output_dict['pass'].lower().strip() == 'ok')
    return output_dict['suggestions'], good_work
    