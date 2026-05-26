import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from open_webui.models.chats import Chats
from open_webui.utils.auth import get_verified_user
from open_webui.utils.chat import generate_chat_completion
from open_webui.env import SRC_LOG_LEVELS


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


DEFAULT_VISION_PROMPT = (
    "Perform OCR on this image and describe its contents in the context of the user query: {query}"
)


class PreprocessFile(BaseModel):
    # 'image' for direct images; 'pdf_page' for already-rendered PDF pages
    # (the frontend renders PDFs to image data URLs via PDF.js — see
    # Chat.svelte:3696-3711 — and posts them here in lieu of the original PDF).
    type: str
    url: str
    filename: Optional[str] = None


class PreprocessVisionForm(BaseModel):
    chat_id: str
    message_id: str
    preprocessor_model_id: str
    vision_prompt: Optional[str] = None
    max_tokens: Optional[int] = 2048
    # 'image' or 'pdf'; controls the prepend tag and prompt header.
    mode: Optional[str] = "image"
    files: list[PreprocessFile]


@router.post("/vision")
async def preprocess_vision(
    request: Request,
    form_data: PreprocessVisionForm,
    user=Depends(get_verified_user),
):
    chat = Chats.get_chat_by_id_and_user_id(form_data.chat_id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found",
        )

    messages_map = (chat.chat or {}).get("history", {}).get("messages", {}) or {}
    user_message = messages_map.get(form_data.message_id)
    if user_message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    if not form_data.files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided",
        )

    user_content = user_message.get("content") or ""
    mode = (form_data.mode or "image").lower()

    prompt_template = form_data.vision_prompt or DEFAULT_VISION_PROMPT
    vision_prompt = prompt_template.replace("{query}", user_content)

    if mode == "pdf":
        text_lead = (
            f"I have uploaded {len(form_data.files)} page(s) from PDF document(s). "
            f"Please analyze them:\n\n{user_content}"
        )
    else:
        text_lead = user_content

    vision_messages = [
        {"role": "system", "content": vision_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text_lead},
                *[
                    {"type": "image_url", "image_url": {"url": f.url}}
                    for f in form_data.files
                ],
            ],
        },
    ]

    try:
        result = await generate_chat_completion(
            request,
            {
                "model": form_data.preprocessor_model_id,
                "messages": vision_messages,
                "stream": False,
                "params": {"max_tokens": form_data.max_tokens or 2048},
            },
            user,
        )
    except Exception as e:
        log.exception("Vision preprocessing failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Vision preprocessing failed: {e}",
        )

    try:
        vision_response = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Preprocessor returned malformed response: {e}",
        )

    if mode == "pdf":
        prepend = f"[PDF Analysis ({len(form_data.files)} pages):\n{vision_response}\n]\n\n"
        processed_flag_key = "pdf_processed"
    else:
        prepend = f"[Vision Analysis:\n{vision_response}\n]\n\n"
        processed_flag_key = "vision_processed"

    # Don't double-prepend if the client retries; the frontend mirrors this
    # guard via `vision_processed`/`pdf_processed` flags on the user message.
    if not user_message.get(processed_flag_key):
        new_content = f"{prepend}{user_content}"
    else:
        new_content = user_content

    updated_message = {
        **user_message,
        "content": new_content,
        processed_flag_key: True,
    }

    Chats.upsert_message_to_chat_by_id_and_message_id(
        form_data.chat_id, form_data.message_id, updated_message
    )

    return {
        "status": True,
        "message_id": form_data.message_id,
        "content": new_content,
        "vision_prompt": vision_prompt,
        "vision_response": vision_response,
    }
