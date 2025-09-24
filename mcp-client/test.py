from openai import OpenAI

client = OpenAI(api_key="YOUR_OPENAI_KEY", base_url="http://localhost:11434/v1")  # <-- real OpenAI API, not Ollama

resp = client.responses.create(
    model="gpt-4o-mini",
    tools=[
        {
            "type": "mcp",
            "server_label": "dice_server",
            "server_url": "http://localhost:8000/mcp/",
            "require_approval": "never",
        },
    ],
    input=(
        "Roll 3 dice with roll_dice, then ask_ollama to write a haiku about the total."
    ),
)

print(resp.output_text)
