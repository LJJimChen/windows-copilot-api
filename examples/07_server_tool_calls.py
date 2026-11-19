"""Example 7 — tool calling through the OpenAI SDK against the server.

Copilot has no native function-calling channel, so the server emulates it: the
tool schemas are injected into the prompt, the model answers with a bare JSON
envelope such as
``{"tool_calls":[{"name":"get_weather","arguments":{"city":"Oslo"}}]}``, and the
server turns that back into OpenAI ``tool_calls``. From the client's point of
view this is an ordinary function-calling loop.

Start the server in another terminal:

    python app.py

Then run this from the project root (needs `pip install openai`):

    python examples/07_server_tool_calls.py
"""

import json

from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

# A tiny local tool the model can call.
def get_weather(city: str) -> str:
    fake = {"Beijing": "3C, clear", "Oslo": "1C, snow", "Lagos": "31C, humid"}
    return fake.get(city, f"No data for {city}")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

HANDLERS = {"get_weather": get_weather}

messages = [{"role": "user", "content": "What's the weather in Oslo and Beijing?"}]

# The loop stays OpenAI-shaped: send messages, run the calls the model asks for,
# append the results, repeat. No conversation_id needed — the server recognizes
# the resent history and continues the same Copilot thread, sending only the new
# tool results upstream.
for turn in range(1, 6):
    response = client.chat.completions.create(
        model="copilot",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    message = response.choices[0].message
    messages.append(message)

    if not message.tool_calls:
        print(f"[turn {turn}] assistant: {message.content}")
        break

    for call in message.tool_calls:
        arguments = json.loads(call.function.arguments or "{}")
        print(f"[turn {turn}] tool call: {call.function.name}({arguments})")
        result = HANDLERS[call.function.name](**arguments)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": result,
        })
else:
    print("gave up after 5 turns")
