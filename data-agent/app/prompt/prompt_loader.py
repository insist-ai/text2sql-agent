from pathlib import Path


def load_prompt(name: str):
    prompts_path = Path(__file__).parents[2] / 'prompts' / f"{name}.prompt"
    return  prompts_path.read_text(encoding="utf-8")
