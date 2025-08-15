from __future__ import annotations

import os
import uuid
import json
import asyncio 
from openai import OpenAI
from supabase import Client
from typing import Any, Dict, Optional, Union, List


from prompts import FLASHCARD_CHUNKER, FLASHCARD_GENERATOR, FLASHCARD_QA, CONTENT_INSTRUCTIONS
# from pydantic_formatting import ChunkPayload, CardsPayload, QAReviewPayload, coerce_and_validate




class OpenAIAgent:

    '''
    Args:
        system_prompt:
        chat_input:
        api_key:
        model:

    '''
    def __init__(
        self,
        system_prompt: str,
        api_key: str,
        uuid: str,
        jwt_token: str, # come back and remove unnecessary variables such as uuid and jwt_token (used in main.py)
        model: str = "gpt-4o-mini",
    ):

        self.agent_instructions  = self.get_system_prompt(system_prompt)
        self.client              = OpenAI(api_key=api_key)
        self.model               = model
        self.uuid                = uuid
        self.jwt_token           = jwt_token

        # Initialize To Avoid ReferenceError
        self.prompt              = None
              

    def get_system_prompt(self, category: str) -> dict:

        return 
    
    
    async def pull_school_details(self, user_id: str):
        table_name = 'canvas_ics_table'
        student_courses = self.supabase_client.table(table_name) \
                              .select('course_name') \
                              .eq("user_id", user_id) \
                              .order("created_at", desc=True) \
                              .execute()
        

        institution_name = self.supabase_client.table('users') \
                              .select('canvas_institution_name, name') \
                              .eq("user_id", user_id) \
                              .execute()
        

        institution = institution_name.data[0]['canvas_institution_name']
        name        = institution_name.data[0]['name']
        courses     = [course['course_name'] for course in student_courses.data]

        output = f"{name} is enrolled at {institution}.\Their current courses:\n"
        for idx, course in enumerate(courses, start=1):
            output += f"  {idx}. {course}\n"

        output += 'If institution or courses are missing, student has not linked their canvas yet'

        return output
    
    async def pull_episodic_memory(self, user_id: str, system_role_id: int):
        table_name = 'agent_memory'
        episodic_memory = self.supabase_client.table(table_name) \
                        .select('conversation_id, summary') \
                        .eq("user_id", user_id) \
                        .eq("system_role_id", system_role_id)\
                        .eq('memory_type', 'episodic') \
                        .execute()
        line_spacer      = '-' * 60
        formatted_string = ''
        episodic_memory  = episodic_memory.data

        for memory in episodic_memory:
            formatted_string += f'Conversation {memory["conversation_id"]}:\n Episodic Memories: {memory["summary"]}\n\n{line_spacer}\n'

        return formatted_string
    
    async def log_automated_suggestions(self, message: str, user_id: str, system_role_id: int, tokens: int):

        # Generate UUID for Message
        uid   = uuid.uuid4()

        # Insert Into Messages
        data = {
            "user_id": user_id,
            "content": message,
            "token_count": tokens,
            "system_role_id": system_role_id
        }

        # Choose Messages Table
        table_name = 'automated_suggestions'

        # Supabase Client Insert and Execute Automated Message
        self.supabase_client.table(table_name)\
                            .insert(data)\
                            .execute()

        return 
    



    async def run(self, user_id: str, jwt_token: str, system_role_id: int, supabase_client: Client, message: str = None) -> dict:
        '''
        Workflow Triggered by the UI Workflow

        Parameters:
            user_id (str):
            jwt_token (str):
            uuid (str): 
            system_role_id (int): 
        '''

        self.supabase_client = supabase_client

        line_spacer = '-' * 60

        # Call Student Details (school and courses) and To Formatted Message
        student_details = self.pull_school_details(user_id = user_id)

        # Pull Any Episodic Memory That Was Stored
        episodic_memory = self.pull_episodic_memory(user_id = user_id, system_role_id = system_role_id)

        # Pull User Questionaires
        lifestyle_priorities, social_priorities = await self.pull_user_questionaires(user_id)

        # Run 2 DB calls/Inserts Asynchronously Since They Are Independent
        student_details, episodic_memory = await asyncio.gather(student_details, episodic_memory)

        # Contextualized Message for Academic Agent
        # Academic Agent
        if system_role_id == 3:
            contextualized_message = f'The following is the students Lifestyle & Health Questionaire:\n\n\n{lifestyle_priorities}. \n\n\n The following is the students Social & Academic Questionaire:\n\n\n {social_priorities}.\n\n\n The following is the students academic profile: \n\n\n {student_details} \n\n\n{line_spacer}\n\n\n The following is the retrieved episodic memory: \n\n {episodic_memory}'
        # Jobs Analysis Agent
        elif system_role_id == 4:
            contextualized_message = f'The following is the students Lifestyle & Health Questionaire:\n\n\n{lifestyle_priorities}. \n\n\n The following is the students Social & Academic Questionaire:\n\n\n {social_priorities}.\n\n\n The following is the students academic profile: \n\n\n {student_details} \n\n\n{line_spacer}\n\n\n The following is the retrieved episodic memory: \n\n {episodic_memory} \n\n\n The following is the extracted skills from a multiple of job opportunities {message}'


        instructions = self.agent_instructions['prompt']

        # The web search tool is activated directly
        tools_config = [{"type": "web_search_preview"}]

        # Parameters for the API call
        api_params = {
            "model": self.model,
            "instructions": instructions,
            "input": contextualized_message,
            "tools": tools_config,
            "stream": True, # The Responses API also supports streaming
            "tool_choice": "required", # require that web tool is used
        }

        response = self.client.responses.create(**api_params)

        # 2. Collect Response and Stream
        collected_response = ''
        new_response_id = None # To store the ID for the next turn

        for event in response: # 'response' object itself acts as an async iterator for streaming
            if hasattr(event, "type"):
                if "text.delta" in event.type:
                    # This is a text chunk
                    content = event.delta
                    if content:
                        collected_response += content
    

        # 8. Store Agent Response
        # await self.log_automated_suggestions(message = collected_response, user_id = user_id, system_role_id = system_role_id)

   
        return collected_response
    

# -------- shared helpers --------

def _extract_output_text(resp: Any) -> str:
    """
    Try multiple plausible shapes from the OpenAI Responses API / Chat API.
    Returns empty string if nothing found.
    """
    # New Responses API often exposes this:
    if hasattr(resp, "output_text") and isinstance(resp.output_text, str):
        return resp.output_text.strip()

    # Fallback: responses with .output -> list of items with .content -> list of parts
    try:
        output = getattr(resp, "output", None)
        if isinstance(output, list):
            parts: List[str] = []
            for item in output:
                content = getattr(item, "content", None)
                if isinstance(content, list):
                    for c in content:
                        # common fields: c.text or c["text"]
                        text = getattr(c, "text", None)
                        if isinstance(text, str):
                            parts.append(text)
            if parts:
                return "\n".join(parts).strip()
    except Exception:
        pass

    # Chat-style fallback
    try:
        choices = getattr(resp, "choices", None)
        if isinstance(choices, list) and choices:
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                return content.strip()
    except Exception:
        pass

    # Last resort: str(resp)
    try:
        return str(resp).strip()
    except Exception:
        return ""


def _extract_json_from_text(full_text: str) -> Optional[dict]:
    """
    Extract a JSON object either from a ```json ... ``` fenced block
    or from the entire text (if it is raw JSON). Returns None if parsing fails.
    """
    if not isinstance(full_text, str) or not full_text:
        return None

    # Try fenced block first
    fence = "```json"
    start = full_text.find(fence)
    if start != -1:
        start += len(fence)
        end = full_text.find("```", start)
        if end != -1:
            candidate = full_text[start:end].strip()
            try:
                return json.loads(candidate)
            except Exception:
                pass

    # Try any fenced code block (without explicit json)
    fence2 = "```"
    start2 = full_text.find(fence2)
    if start2 != -1:
        start2 += len(fence2)
        end2 = full_text.find("```", start2)
        if end2 != -1:
            candidate2 = full_text[start2:end2].strip()
            try:
                return json.loads(candidate2)
            except Exception:
                pass

    # Try raw JSON
    try:
        return json.loads(full_text)
    except Exception:
        return None


# ============ Chunk Splitter ============

class ChunkSplitterAgent(OpenAIAgent):
    def __init__(
        self,
        system_prompt: str,
        api_key: str,
        uuid: str,
        jwt_token: str,
        model: str = "gpt-5-mini",
    ):
        self.agent_instructions = self.get_system_prompt()  # uses constants
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.uuid = uuid
        self.jwt_token = jwt_token
        self.prompt: Optional[str] = None  # avoid NameError

    def get_system_prompt(self) -> Dict[str, Optional[str]]:
        return {"prompt": FLASHCARD_CHUNKER, "schema": None}

    async def pull_user_questionaires(self, user_id: str):
        return

    async def run(
        self,
        user_id: str,
        jwt_token: str,
        system_role_id: int,
        supabase_client: "Client",
        message: Optional[str] = None,
        university_information: str = "",
    ) -> Union[dict, str]:
        
        self.supabase_client = supabase_client
        instructions = self.agent_instructions["prompt"]
        # response_format =  {
        #     "type": "json_schema",
        #     "json_schema": ChunkPayload,
        #     "strict": True
        # }

        api_params = {
            "model": self.model,
            "instructions": instructions,
            "input": message or "",
            "stream": False
        }

        resp = self.client.responses.create(**api_params)


        input_tokens     = resp.usage.input_tokens
        output_tokens    = resp.usage.output_tokens

        await self.log_automated_suggestions(
                message=message, user_id=user_id, system_role_id=system_role_id, tokens = input_tokens
            )

        try:
            full_text = _extract_output_text(resp)

            # Try to parse JSON (chunker should return JSON)
            parsed = _extract_json_from_text(full_text) or {}
            await self.log_automated_suggestions(
                message=full_text, user_id=user_id, system_role_id=system_role_id, tokens = output_tokens
            )
            # Prefer returning parsed JSON when available
            return parsed if parsed else full_text

        except Exception as e:
            print(f"Error handling response: {e}")
            print(f"Full response object: {resp}")
            return {}
        

class ContentInstructionAgent(OpenAIAgent):
    def __init__(
        self,
        api_key: str,
        uuid: str,
        jwt_token: str,
        model: str = "gpt-5-mini",
    ):
        self.agent_instructions = self.get_system_prompt()
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.uuid = uuid
        self.jwt_token = jwt_token
        self.prompt: Optional[str] = None

    def get_system_prompt(self) -> Dict[str, Optional[str]]:
        return {"prompt": CONTENT_INSTRUCTIONS, "schema": None}

    async def pull_user_questionaires(self, user_id: str):
        return

    async def run(
        self,
        user_id: str,
        jwt_token: str,
        system_role_id: int,
        supabase_client: "Client",
        message: Optional[str] = None,
        university_information: str = "",
    ) -> Union[dict, str]:
        self.supabase_client = supabase_client

        instructions = self.agent_instructions["prompt"]

        api_params = {
            "model": self.model,
            "instructions": instructions,
            "input": message or "",
            "stream": False,
        }

        resp = self.client.responses.create(**api_params)

        input_tokens     = resp.usage.input_tokens
        output_tokens    = resp.usage.output_tokens

        await self.log_automated_suggestions(
                message=message, user_id=user_id, system_role_id=system_role_id, tokens = input_tokens
            )

        try:
            full_text = _extract_output_text(resp)

            # Try to parse JSON (chunker should return JSON)
            parsed = _extract_json_from_text(full_text) or {}
            await self.log_automated_suggestions(
                message=full_text, user_id=user_id, system_role_id=system_role_id, tokens = output_tokens
            )
            # Prefer returning parsed JSON when available
            return parsed if parsed else full_text

        except Exception as e:
            print(f"Error handling response: {e}")
            print(f"Full response object: {resp}")
            return {}


# ============ Flashcard Generator ============

class FlashcardGeneratorAgent(OpenAIAgent):
    def __init__(
        self,
        system_prompt: str,
        api_key: str,
        uuid: str,
        jwt_token: str,
        model: str = "gpt-5-mini",
    ):
        self.agent_instructions = self.get_system_prompt()
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.uuid = uuid
        self.jwt_token = jwt_token
        self.prompt: Optional[str] = None

    def get_system_prompt(self) -> Dict[str, Optional[str]]:
        return {"prompt": FLASHCARD_GENERATOR, "schema": None}

    async def run(
        self,
        user_id: str,
        jwt_token: str,
        system_role_id: int,
        supabase_client: "Client",
        message: Optional[str] = None,
        university_information: str = "",
    ) -> dict:
        self.supabase_client = supabase_client

        instructions = self.agent_instructions["prompt"]

        api_params = {
            "model": self.model,
            "instructions": instructions,
            "input": message or "",
            "stream": False,
        }

        resp = self.client.responses.create(**api_params)

        input_tokens     = resp.usage.input_tokens
        output_tokens    = resp.usage.output_tokens
        
        await self.log_automated_suggestions(
                message=message, user_id=user_id, system_role_id=system_role_id, tokens = input_tokens
            )

        try:
            full_text = _extract_output_text(resp)
            json_obj = _extract_json_from_text(full_text)
            if json_obj is None:
                # Log raw text to inspect prompt alignment
                await self.log_automated_suggestions(
                    message=full_text, user_id=user_id, system_role_id=system_role_id, tokens = output_tokens
                )
                return {}

            await self.log_automated_suggestions(
                message=json.dumps(json_obj)[:8000],  # cap size
                user_id=user_id,
                system_role_id=system_role_id,
                tokens = output_tokens
            )
            return json_obj

        except Exception as e:
            print(f"Error handling response: {e}")
            print(f"Full response object: {resp}")
            return {}


# ============ Flashcard QA/Dedupe ============

class FlashcardQualityAgent(OpenAIAgent):
    def __init__(
        self,
        system_prompt: str,
        api_key: str,
        uuid: str,
        jwt_token: str,
        model: str = "gpt-5-mini",
    ):
        self.agent_instructions = self.get_system_prompt()
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.uuid = uuid
        self.jwt_token = jwt_token
        self.prompt: Optional[str] = None

    def get_system_prompt(self) -> Dict[str, Optional[str]]:
        return {"prompt": FLASHCARD_QA, "schema": None}

    async def pull_user_questionaires(self, user_id: str):
        return

    async def run(
        self,
        user_id: str,
        jwt_token: str,
        system_role_id: int,
        supabase_client: "Client",
        message: Optional[str] = None,
        university_information: str = "",
    ) -> Union[dict, str]:
        self.supabase_client = supabase_client

        instructions = self.agent_instructions["prompt"]

        api_params = {
            "model": self.model,
            "instructions": instructions,
            "input": message or "",
            "stream": False,
        }

        resp = self.client.responses.create(**api_params)

        input_tokens     = resp.usage.input_tokens
        output_tokens    = resp.usage.output_tokens

        await self.log_automated_suggestions(
                message=message, user_id=user_id, system_role_id=system_role_id, tokens = input_tokens
            )

        try:
            full_text = _extract_output_text(resp)
            parsed = _extract_json_from_text(full_text) or {}
            await self.log_automated_suggestions(
                message=full_text, user_id=user_id, system_role_id=system_role_id, tokens = output_tokens
            )
            return parsed if parsed else full_text

        except Exception as e:
            print(f"Error handling response: {e}")
            print(f"Full response object: {resp}")
            return {}