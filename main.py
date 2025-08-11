import json, os
from pathlib import Path
import tkinter as tk
from tkinter import scrolledtext, messagebox
from dotenv import load_dotenv
from openai import OpenAI
import threading
import sys

def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

APP_DIR = Path(__file__).parent
STORY_PATH = APP_DIR / "story.json"

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------- Validation ----------
def load_story():
    if not STORY_PATH.exists():
        raise FileNotFoundError(f"Story file not found: {STORY_PATH}")
    try:
        story = json.loads(STORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Story file is not valid JSON: {e}")

    if not isinstance(story, dict):
        raise ValueError("Top-level story JSON must be an object mapping id -> node")

    for node_id, node in story.items():
        if "text" not in node or not isinstance(node["text"], str):
            raise ValueError(f"Node '{node_id}' must have string 'text'")
        if "options" in node and not isinstance(node["options"], list):
            raise ValueError(f"Node '{node_id}' 'options' must be a list if present")

    for node_id, node in story.items():
        for opt in node.get("options", []):
            nxt = opt.get("next")
            if nxt and nxt not in story:
                raise ValueError(f"Node '{node_id}' option points to unknown node '{nxt}'")
    
    for node_id, node in story.items():
        has_opts = bool(node.get("options"))
        has_hint = bool(node.get("next_hint"))

        nm = node.get("next_map")
        nxt = node.get("next")

        if not has_opts and has_hint:
            if not nm and not nxt:
                raise ValueError(
                    f"Node '{node_id}' has next_hint but no 'next_map' or 'next' to route choices."
                )
            
        if nm is not None:
            if not isinstance(nm, list):
                raise ValueError(f"Node '{node_id}' next_map must be a list")
            for t in nm:
                if not isinstance(t,str):
                    raise ValueError(f"Node '{node_id}' next_map entries must be strings")
                if t not in story:
                    raise ValueError(f"Node '{node_id}' next_map points to unknown node '{t}'")
                
        if nxt is not None and nxt not in story:
            raise ValueError(f"Node '{node_id}' 'next' points to unknown node '{nxt}'")

    return story

# ---------- AI Logic----------
LORE = (
    "LORE RULES:\n"
    "- Setting: Low-magic forest frontier; medieval tech.\n"
    "- POV: second-person ('you'); no modern slang.\n"
    "- Do NOT contradict authored text or outcomes.\n"
    "- 120–180 words. End with exactly 2–3 numbered options.\n"
    "- Options short (<12 words) and mutually exclusive.\n"
)

def ai_bridge_json(prev_text: str, next_hint: str):
    """
    Ask the model to return STRICT JSON so we can parse reliably.
    """
    prompt = (
        f"{LORE}\n"
        "TASK:\n"
        "Write a short scene that continues from the authored text and steers toward the hint.\n"
        "Length: 120–180 words. Then provide exactly TWO brief choices (<= 12 words each).\n\n"
        f"AUTHORED_TEXT:\n{prev_text}\n\n"
        f"NEXT_HINT:\n{next_hint}\n\n"
        "OUTPUT FORMAT (STRICT):\n"
        '{ "scene": "SCENE_TEXT", "options": ["CHOICE_1", "CHOICE_2"] }\n'
        "RULES:\n"
        "- Respond ONLY with a single JSON object as above.\n"
        "- No extra text, no markdown, no code fences.\n"
        "- Use plain ASCII quotes and characters.\n"
    )
    resp = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        timeout=30 
    )
    return resp.output_text.strip()

def parse_scene_and_options_json(text: str):
    """
    Parse model output as JSON with keys: scene (str), options (list[str]).
    Includes a small recovery if the model adds stray text around the JSON.
    """
    try:
        data = json.loads(text)
    except Exception:
        import re as _re
        m = _re.search(r'\{.*\}', text, _re.DOTALL)
        if not m:
            return "", []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return "", []

    scene = data.get("scene", "")
    options = data.get("options", [])
    if not isinstance(scene, str):
        scene = ""
    if not (isinstance(options, list) and all(isinstance(x, str) for x in options)):
        options = []
    options = options[:2]
    return scene.strip(), [o.strip() for o in options]

# ---------- UI ----------
def show_node(node_id, box, btn1, btn2, story, status):
    node = story[node_id]
    status.config(text=f"Node: {node_id}")

    def render_text(t):
        box.config(state="normal")
        box.delete("1.0", tk.END)
        box.insert(tk.END, t)
        box.config(state="disabled")

    def set_buttons(opts):
        if len(opts) >= 1:
            btn1.config(
                text=opts[0]["label"],
                command=lambda: show_node(opts[0]["next"], box, btn1, btn2, story, status)
            )
            btn1.pack(side="left", padx=6)
        else:
            btn1.pack_forget()

        if len(opts) >= 2:
            btn2.config(
                text=opts[1]["label"],
                command=lambda: show_node(opts[1]["next"], box, btn1, btn2, story, status)
            )
            btn2.pack(side="left", padx=6)
        else:
            btn2.pack_forget()

    opts = node.get("options", [])
    if opts:
        render_text(node["text"])
        set_buttons(opts)
        return

    hint = node.get("next_hint", "")
    if not hint:
        render_text(node["text"])
        set_buttons([])
        return
    
    render_text(node["text"] + "\n\n(Generating…)")
    btn1.pack_forget(); btn2.pack_forget()

    def work():
        try:
            print("[AI] start")
            out = ai_bridge_json(node["text"], hint)
            print("[AI] got output length:", len(out))
            scene, ai_opts = parse_scene_and_options_json(out)
            print("[AI] parsed options:", ai_opts)

            if "next_map" in node and isinstance(node["next_map"], list) and node["next_map"]:
                targets = [nid for nid in node["next_map"] if isinstance(nid, str)]
            elif isinstance(node.get("next"), str):
                targets = [node["next"]] * max(1, len(ai_opts))  
            else:
                targets = [node_id] * max(1, len(ai_opts)) 

            mapped = []
            for idx, label in enumerate(ai_opts):
                if idx < len(targets) and targets[idx] in story:
                    mapped.append({"label": label, "next": targets[idx]})
            print("[AI] mapped:", mapped)

            def update():
                debug_lines = []
                if not ai_opts:
                    debug_lines.append("\n\n[Debug] No options parsed from model output.")
                if "next_map" in node and not node.get("next_map"):
                    debug_lines.append("[Debug] Node has empty next_map.")
                if "next" not in node and "next_map" not in node:
                    debug_lines.append("[Debug] Node has no next/next_map; using loopback.")

                render_text(node["text"] + "\n\n" + scene + ("".join("\n" + d for d in debug_lines)))
                if mapped:
                    set_buttons(mapped[:2]) 
                else:
                    if ai_opts:
                        set_buttons([{"label": ai_opts[0], "next": node_id}] +
                                    ([{"label": ai_opts[1], "next": node_id}] if len(ai_opts) > 1 else []))
                    else:
                        set_buttons([{"label": "Continue", "next": node_id}])
            box.after(0, update)
        except Exception as e:
            box.after(0, lambda: messagebox.showerror("AI Error", str(e)))
    threading.Thread(target=work, daemon=True).start()

def build_ui():
    try:
        story = load_story()
    except Exception as e:
        messagebox.showerror("Story Load Error", str(e))
        raise SystemExit(1)

    root = tk.Tk()
    root.title("Enter The Forest")

    tk.Label(root, text="Story").pack(anchor="w", padx=10, pady=(10, 0))
    box = scrolledtext.ScrolledText(root, wrap="word", height=20, width=70)
    box.pack(fill="both", expand=True, padx=10, pady=10)
    box.config(state="disabled")

    status = tk.Label(root, text="", anchor="w")
    status.pack(fill="x", padx=10, pady=(0,10))
    btnbar = tk.Frame(root)
    btnbar.pack(pady=(0, 10), anchor="center") 

    btn1 = tk.Button(btnbar, text="Option 1")
    btn2 = tk.Button(btnbar, text="Option 2")

    show_node("start", box, btn1, btn2, story, status)

    root.minsize(800, 600)
    return root

if __name__ == "__main__":
    build_ui().mainloop()
