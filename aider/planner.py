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

# Available Task Types:
- **user**: Ask user for more details until you have enough information to plan a task.
- **reason**: Speak to user when no more coding work is needed to be done. Usually as a final task.
- **code**: Write a piece of code to achieve one specific goal.
- **run**: run shell commands to achieve one specific goal.
- **reflect**: reflect on previous task results, and do the rest work to make sure to achieve the goal.
- **other**: Any tasks not in the defined categories

# Task:
Based on the user's goal or the user's existing plan, write a simple plan or modify the existing plan of what you should do \
to achieve the goal. A complete plan consists of one to four tasks. The number of tasks CANNOT exceed 6.

**The final task must be a reflect task.**

Output a list of jsons following the format:
```json
[
    {{
        "task_id": str = "unique identifier for a task in plan, can be an ordinal",
        "dependent_task_ids": list[str] = "ids of tasks prerequisite to this task",
        "instruction": "what you should do in this task, composed of two to three imperative sentences. \
Do not use transitional words such as 'first', 'then', or 'last' in the description, \
as the order relationship has already been described using task_id.",
        "task_type": "type of this task, should be one of Available Task Types",
    }},
    ...
]
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


class DAGPlanner(Agent, AgentEnvMixin):

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
             mid_messages: List[Dict[str, str]] = None,
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
            stop=['Observation:', 'Observation:\n'],
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
        return llm_result
        
def update_plan(llm_result: str, user_request: str = None, curr_plan: Plan = None) -> Plan:
    tasks_text = parse_code(text = llm_result, lang = 'json')
    logger.info(f'tasks: {tasks_text}')
    tasks = json5.loads(tasks_text)
    tasks = [Task(**task) for task in tasks]
    if curr_plan is None:
        if user_request is None:
            raise RuntimeError
        new_plan = Plan(goal=user_request)
        new_plan.add_tasks(tasks=tasks)
        return new_plan, True
    else:
        if len(tasks) == 1 or tasks[0].dependent_task_ids:
            if tasks[0].dependent_task_ids and len(tasks) > 1:
                logger.warning(
                    'Current plan will take only the first generated task if the generated tasks are not a '
                    'complete plan')
            if curr_plan.has_task_id(tasks[0].task_id):
                curr_plan.replace_task(tasks[0])
            else:
                curr_plan.append_task(tasks[0])
        else:
            curr_plan.add_tasks(tasks)
        return curr_plan, (len(tasks) >= 1)

def dump_plan_description(plan: Plan, as_prompt = True) -> str:
    tasks = plan.tasks
    output = f"goal: {plan.goal}\n\n"
    curr_id = plan.current_task_id
    curr_instruction = ""
    for task in tasks:
        if task.task_id == curr_id:
            curr_instruction = task.instruction
        output += f"- Task {task.task_id}"
        if task.dependent_task_ids:
            output += f" (dependent on task {', '.join(task.dependent_task_ids)})"
        output += f": {task.instruction}. "
        if task.is_finished:
            output += f"This task has been completed"
            if task.result:
                output += f". The result information is {task.result}\n"
            else:
                output += ".\n"
        else:
            output += f"This task has not been completed yet"
            if task.result:
                output += f". The intermediate information is {task.result}\n"
            else:
                output += ".\n"
        
    output += (
        f"\nWe are currently on the task {plan.current_task_id}: {curr_instruction}. "
    )
    if as_prompt:
        output += (
            "Focus on the current task and *DO NOT* perform other tasks. "
            "Your answer is just a small part of a longer conversation, "
            "so **DO NOT** rush to do everything at one time!"
        )
    return output

def is_all_task_done(plan: Plan) -> bool:
    return all([task.is_finished for task in plan.tasks])