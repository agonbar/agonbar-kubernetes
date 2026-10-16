"""Probe a LiteLLM proxy for the endpoints the ai_task platform needs.

Runs inside the Home Assistant container: reads the URL and key from the
existing `litellm` config entry, then checks structured output and image
generation. Prints no credentials.
"""

import asyncio
import json
import sys

import openai

ENTRIES = "/config/.storage/core.config_entries"
SCHEMA = {
    "name": "casa",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "habitacion": {"type": "string"},
            "encendida": {"type": "boolean"},
        },
        "required": ["habitacion", "encendida"],
        "additionalProperties": False,
    },
}


async def main(model: str) -> int:
    entry = next(
        e
        for e in json.load(open(ENTRIES))["data"]["entries"]
        if e["domain"] == "litellm"
    )
    client = openai.AsyncOpenAI(
        base_url=entry["data"]["url"],
        api_key=entry["data"].get("api_key", "sk-no-key-required"),
    )
    print(f"proxy: {entry['data']['url']}  modelo: {model}")

    print("\n[1] /v1/chat/completions con response_format=json_schema")
    try:
        result = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "La luz del salon esta encendida. Responde en JSON.",
                }
            ],
            response_format={"type": "json_schema", "json_schema": SCHEMA},
        )
    except openai.OpenAIError as err:
        print(f"    FALLA: {type(err).__name__}: {str(err)[:300]}")
    else:
        text = result.choices[0].message.content or ""
        print(f"    respuesta cruda: {text[:200]}")
        try:
            print(f"    JSON valido: {json.loads(text)}")
        except json.JSONDecodeError as err:
            print(f"    NO es JSON valido: {err}")

    print("\n[2] /v1/images/generations")
    try:
        response = await client.images.generate(model=model, prompt="un gato", n=1)
    except openai.OpenAIError as err:
        print(f"    no disponible: {type(err).__name__}: {str(err)[:200]}")
    else:
        image = response.data[0] if response.data else None
        print(
            f"    responde: imagenes={len(response.data or [])} "
            f"b64={bool(image and image.b64_json)} "
            f"url={bool(image and image.url)} "
            f"output_format={response.output_format} size={response.size}"
        )
        if image is None or not (image.b64_json or image.url):
            print("    -> respuesta vacia: no configures modelo de imagen aqui")
    return 0


sys.exit(asyncio.run(main(sys.argv[1])))
