import random
import requests
from fastmcp import FastMCP

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.1"

mcp = FastMCP(name="Dice Roller", stateless_http=True)

@mcp.tool
def roll_dice(n_dice: int) -> list[int]:
    """Roll `n_dice` 6-sided dice and return the results."""
    print(f"Rolling {n_dice} dice...")
    return [random.randint(1, 6) for _ in range(n_dice)]

@mcp.tool
def sum_dice(rolls: list[int]) -> int:
    """Sum a list of dice rolls. use data type 'list[int]' for input."""
    return sum(rolls)

# @mcp.tool
# def ask_ollama(prompt: str) -> str:
#     """Send a prompt to Ollama and return the response."""
#     r = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt})
#     r.raise_for_status()
#     return r.json()["response"]

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)
