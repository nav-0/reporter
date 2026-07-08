import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from pypdf import PdfReader
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.widgets import Box, Frame, Label, TextArea


TEMPLATE = """# Starship Specification

## Vessel Overview

Describe the starship, its purpose, mission profile, and operating context.

## Propulsion System

Describe the propulsion model, energy source, engine design, and known constraints.

## Crew and Passenger Model

Describe who uses the vessel, who operates it, and what access or responsibility each group has.

## Cargo and Alien Artifacts

Describe the types of cargo, unusual materials, alien objects, and handling requirements. Provide at least one photo and/or a written description for this section.

## Emergency Pancake Protocol

Describe emergency procedures involving breakfast systems, pancake containment, syrup routing, and crew safety.
"""

REPORTS_DIR = Path("spaceships")
TMP_REPORTS_DIR = Path("tmp") / "reports"


def ask_copilot(prompt: str) -> str:
    result = subprocess.run(
        [
            "copilot",
            "-p",
            prompt
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    return result.stdout


def extract_markdown_output(raw_output: str) -> str:
    # Prefer fenced markdown blocks when the CLI wraps content in logs.
    fenced_blocks = re.findall(
        r"```(?:markdown|md)?\\s*\\n(.*?)\\n```",
        raw_output,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced_blocks:
        return fenced_blocks[-1].strip() + "\n"

    # Fall back to the first markdown heading if extra text was prepended.
    lines = raw_output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#"):
            return "\n".join(lines[index:]).strip() + "\n"

    # If no clear marker exists, return trimmed output as-is.
    return raw_output.strip() + "\n"


def parse_sections(template: str):
    matches = list(re.finditer(r"^##\s+(.+)$", template, flags=re.MULTILINE))
    sections = []

    for i, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(template)
        content = template[start:end].strip()
        sections.append({
            "title": title,
            "template": content
        })

    return sections


def slugify(value: str):
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")


def progress_bar(current: int, total: int):
    width = 15
    filled = int(width * current / total)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {current}/{total}"


def loading_animation(stop_event: threading.Event):
    """Display a loading animation while waiting for Copilot."""
    frames = ["|  Analyzing...", "/  Analyzing...", "-  Analyzing...", "\\  Analyzing.."]
    i = 0
    while not stop_event.is_set():
        print(f"\r{frames[i % len(frames)]}", end="", flush=True)
        time.sleep(0.1)
        i += 1
    print("\r" + " " * 24 + "\r", end="", flush=True)


def is_image(path: str):
    return Path(path).suffix.lower() in [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"]


def is_pdf(path: str):
    return Path(path).suffix.lower() == ".pdf"


def copy_to_assets(file_path: str, report_dir: Path) -> str:
    """Copy a file to the assets folder and return the new local path."""
    assets_dir = report_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    
    source = Path(file_path)
    if not source.exists():
        return file_path

    # Normalize filenames for stable markdown links across platforms/previews.
    base_name = slugify(source.stem) or "asset"
    suffix = source.suffix.lower()
    candidate = assets_dir / f"{base_name}{suffix}"
    counter = 2
    while candidate.exists() and candidate.resolve() != source.resolve():
        candidate = assets_dir / f"{base_name}-{counter}{suffix}"
        counter += 1

    shutil.copy2(source, candidate)
    return f"assets/{candidate.name}"


def is_text_file(path: str):
    return Path(path).suffix.lower() in [".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".log"]


def extract_pdf_text(path: str):
    reader = PdfReader(path)
    text = []
    for page in reader.pages:
        text.append(page.extract_text() or "")
    return "\n".join(text)


def extract_file_text(path: str):
    if is_pdf(path):
        return extract_pdf_text(path)

    if is_text_file(path):
        return Path(path).read_text()

    return f"[Artifact supplied but not text-extracted: {path}]"


def parse_input_into_text_and_files(raw_text: str):
    text_lines = []
    file_paths = []

    for line in raw_text.splitlines():
        stripped = line.strip()

        possible_paths = []
        try:
            possible_paths = shlex.split(stripped)
        except ValueError:
            possible_paths = [stripped]

        if len(possible_paths) == 1 and Path(possible_paths[0]).exists():
            file_paths.append(possible_paths[0])
        elif stripped and Path(stripped).exists():
            file_paths.append(stripped)
        else:
            text_lines.append(line)

    text = "\n".join(text_lines).strip()
    return text, file_paths


def render_evidence(section_evidence):
    lines = []

    if section_evidence["text"]:
        lines.append(f"Text entries: {len(section_evidence['text'])}")

    if section_evidence["files"]:
        lines.append("Documents:")
        for item in section_evidence["files"]:
            lines.append(f"- {item} (text will be extracted and processed)")

    if section_evidence["document_images"]:
        lines.append("Images:")
        for item in section_evidence["document_images"]:
            lines.append(f"- {item} (image will be included in final draft)")

    if not lines:
        return "(none yet)"

    return "\n".join(lines)


def normalize_markdown_image_paths(markdown: str) -> str:
    """Decode URL-encoded assets links in markdown image paths."""
    return re.sub(
        r"\((assets/[^)]+)\)",
        lambda match: f"({unquote(match.group(1))})",
        markdown,
    )





def collect_section(section, section_number, total_sections, evidence):
    kb = KeyBindings()

    input_area = TextArea(
        text="",
        multiline=True,
        scrollbar=True,
        wrap_lines=False,
        prompt="",
    )

    template_area = TextArea(
        text=section["template"],
        multiline=True,
        scrollbar=True,
        wrap_lines=True,
        read_only=True,
    )

    evidence_area = TextArea(
        text=render_evidence(evidence),
        multiline=True,
        scrollbar=True,
        wrap_lines=True,
        read_only=True,
    )

    def update_evidence_preview(_):
        text, file_paths = parse_input_into_text_and_files(input_area.text)
        preview_evidence = {
            "text": list(evidence["text"]),
            "files": list(evidence["files"]),
            "document_images": list(evidence["document_images"]),
        }

        if text:
            preview_evidence["text"].append(text)

        for path in file_paths:
            if is_image(path):
                preview_evidence["document_images"].append(path)
            else:
                preview_evidence["files"].append(path)

        evidence_area.text = render_evidence(preview_evidence)

    input_area.buffer.on_text_changed += update_evidence_preview

    title_text = f"SECTION {section_number}/{total_sections}: {section['title'].upper()}"
    progress_text = f"Progress: {progress_bar(section_number - 1, total_sections)}"
    total_line_width = 98
    pad = max(2, total_line_width - len(title_text) - len(progress_text))
    header_line = f"{title_text}{' ' * pad}{progress_text}"
    border = "═" * (len(header_line) + 4)

    header = Label(
        text=f"\n╔{border}╗\n║  {header_line}  ║\n╚{border}╝\n",
        style="bold"
    )

    command_footer = Label(
        text="Commands: Drag/drop files into Data Entry | Ctrl-N = next section | Ctrl-S = save and quit | Ctrl-C = quit without saving progress"
    )

    @kb.add("c-n")
    def _(event):
        event.app.exit(result={"action": "next", "text": input_area.text})

    @kb.add("c-s")
    def _(event):
        event.app.exit(result={"action": "save_quit", "text": input_area.text})

    @kb.add("c-c")
    def _(event):
        event.app.exit(result={"action": "quit_without_save", "text": input_area.text})

    left = HSplit([
        Frame(input_area, title="Data Entry (Drop Zone) - type notes and drag/drop images/documents here"),
        Frame(evidence_area, title="Current Evidence & Assets"),
    ])

    right = Frame(template_area, title="Template Section Preview")

    root_container = HSplit([
        header,
        VSplit([
            Box(left, padding=1),
            Box(right, padding=1),
        ]),
        command_footer
    ])

    app = Application(
        layout=Layout(root_container),
        key_bindings=kb,
        full_screen=True,
    )

    return app.run()


def empty_evidence(sections):
    return {
        section["title"]: {
            "text": [],
            "files": [],
            "document_images": [],
        }
        for section in sections
    }


def marker_path_for(report_dir: Path) -> Path:
    return TMP_REPORTS_DIR / f"{report_dir.name}.pending.json"


def save_progress_state(state):
    report_dir = Path(state["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "assets").mkdir(exist_ok=True)

    progress_path = report_dir / "progress.json"
    progress_path.write_text(json.dumps(state, indent=2))

    TMP_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    marker_payload = {
        "name": state["name"],
        "report_dir": state["report_dir"],
        "saved_at": int(time.time()),
    }
    marker_path_for(report_dir).write_text(json.dumps(marker_payload, indent=2))


def remove_progress_state(report_dir: Path):
    progress_path = report_dir / "progress.json"
    if progress_path.exists():
        progress_path.unlink()

    marker = marker_path_for(report_dir)
    if marker.exists():
        marker.unlink()


def list_unfinished_reports():
    if not TMP_REPORTS_DIR.exists():
        return []

    reports = []
    for marker in sorted(TMP_REPORTS_DIR.glob("*.pending.json")):
        try:
            data = json.loads(marker.read_text())
        except json.JSONDecodeError:
            continue

        report_dir = Path(data.get("report_dir", ""))
        progress_path = report_dir / "progress.json"
        if not progress_path.exists():
            continue

        try:
            progress_data = json.loads(progress_path.read_text())
            current_index = int(progress_data.get("current_section_index", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            current_index = 0

        reports.append({
            "name": data.get("name", report_dir.name),
            "report_dir": str(report_dir),
            "current_section_index": current_index,
        })

    return reports


def create_new_report_state(sections):
    name = input("Enter spaceship name: ").strip()
    while not name:
        name = input("Spaceship name is required. Enter spaceship name: ").strip()

    report_id = f"{slugify(name) or 'spaceship'}-{int(time.time())}"
    report_dir = REPORTS_DIR / report_id
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "assets").mkdir(exist_ok=True)

    state = {
        "name": name,
        "report_dir": str(report_dir),
        "current_section_index": 0,
        "all_evidence": empty_evidence(sections),
    }
    save_progress_state(state)
    return state


def choose_report_state(sections):
    unfinished = list_unfinished_reports()

    print("\nSelect an option:")
    print("1. Start a new spaceship report")
    if unfinished:
        print("2. Continue an unfinished report")
    print("q. Quit")

    choice = input("Choice: ").strip().lower()
    while choice not in (["1", "2", "q"] if unfinished else ["1", "q"]):
        choice = input("Enter a valid choice: ").strip().lower()

    if choice == "q":
        return None

    if choice == "1":
        return create_new_report_state(sections)

    print("\nUnfinished reports:")
    for idx, item in enumerate(unfinished, start=1):
        print(
            f"{idx}. {item['name']} "
            f"(progress: completed {item['current_section_index']} sections)"
        )

    selected = input("Select report number: ").strip()
    valid_numbers = {str(i) for i in range(1, len(unfinished) + 1)}
    while selected not in valid_numbers:
        selected = input("Enter a valid report number: ").strip()

    chosen = unfinished[int(selected) - 1]
    progress_path = Path(chosen["report_dir"]) / "progress.json"
    return json.loads(progress_path.read_text())


def print_bill_of_evidence(sections, all_evidence):
    print("\n" + "=" * 80)
    print("BILL OF EVIDENCE")
    print("=" * 80)

    for section in sections:
        title = section["title"]
        evidence = all_evidence[title]

        print(f"\n## {title}")
        print("-" * 80)
        print(render_evidence(evidence))


def build_generation_prompt(sections, all_evidence):
    prompt_parts = []

    prompt_parts.append("Generate a clean markdown first draft using the template and evidence below.")
    prompt_parts.append("Use only the supplied evidence. Do not invent missing details.")
    prompt_parts.append("If a section has little or no evidence, keep it simple and do not fabricate.")
    prompt_parts.append(
        "If evidence is sparse, ambiguous, malformed, or unverified, preserve it under an "
        "'Unverified Notes' subsection in the relevant section instead of omitting it."
    )
    prompt_parts.append("Use image paths exactly as provided. Do not URL-encode paths.")
    prompt_parts.append("\n\nTEMPLATE:\n")
    prompt_parts.append(TEMPLATE)

    prompt_parts.append("\n\nEVIDENCE BY SECTION:\n")

    for section in sections:
        title = section["title"]
        evidence = all_evidence[title]

        prompt_parts.append(f"\n\n## {title}\n")

        if evidence["text"]:
            prompt_parts.append("\nUSER TEXT NOTES:\n")
            for i, text in enumerate(evidence["text"], start=1):
                prompt_parts.append(f"\n--- Text Entry {i} ---\n{text}\n")

        if evidence["files"]:
            prompt_parts.append("\nFILE EVIDENCE:\n")
            for path in evidence["files"]:
                extracted = extract_file_text(path)
                prompt_parts.append(f"\n--- File: {path} ---\n{extracted}\n")

        if evidence["document_images"]:
            prompt_parts.append("\nIMAGES TO INCLUDE IN FINAL DOCUMENT:\n")
            for path in evidence["document_images"]:
                prompt_parts.append(f"- {path}\n")

    prompt_parts.append(
        "\n\nReturn only the completed markdown draft and nothing else. "
        "Do not include explanations, notes, logs, metadata, code fences, or any surrounding text."
    )

    return "\n".join(prompt_parts)


def main():
    if not sys.stdin.isatty():
        print("This program requires an interactive terminal (TTY) for the full-screen UI.")
        print("Run it directly in a real terminal, not in a piped/scripted/non-interactive context.")
        return 1

    sections = parse_sections(TEMPLATE)

    state = choose_report_state(sections)
    if state is None:
        print("Exited.")
        return 0

    report_dir = Path(state["report_dir"])
    all_evidence = state["all_evidence"]
    current_section_index = int(state.get("current_section_index", 0))

    print(f"\nWorking report: {state['name']}")
    print(f"Directory: {report_dir.resolve()}\n")

    for index in range(current_section_index, len(sections)):
        section = sections[index]
        title = section["title"]
        evidence = all_evidence[title]

        section_result = collect_section(
            section=section,
            section_number=index + 1,
            total_sections=len(sections),
            evidence=evidence
        )

        action = section_result["action"]
        raw_input_text = section_result["text"]

        if action in ["next", "save_quit"]:
            text, files = parse_input_into_text_and_files(raw_input_text)

            if text:
                evidence["text"].append(text)

            for path in files:
                if is_image(path):
                    local_path = copy_to_assets(path, report_dir)
                    evidence["document_images"].append(local_path)
                else:
                    evidence["files"].append(path)

        state["all_evidence"] = all_evidence

        if action == "next":
            state["current_section_index"] = index + 1
            save_progress_state(state)
            continue

        if action == "save_quit":
            state["current_section_index"] = index + 1
            save_progress_state(state)
            print("\nProgress saved. You can continue this report later from the startup menu.")
            return 0

        if action == "quit_without_save":
            print("\nQuit without saving progress.")
            return 0

    print_bill_of_evidence(sections, all_evidence)

    generate = input("\nGenerate first draft with Copilot? [y/n]: ").strip().lower()

    if generate == "y":
        print("\nRequesting analysis from Copilot...")
        prompt = build_generation_prompt(sections, all_evidence)
        stop_spinner = threading.Event()
        spinner_thread = threading.Thread(target=loading_animation, args=(stop_spinner,), daemon=True)
        spinner_thread.start()
        try:
            raw_draft = ask_copilot(prompt)
        finally:
            stop_spinner.set()
            spinner_thread.join()
        draft = extract_markdown_output(raw_draft)
        draft = normalize_markdown_image_paths(draft)

        output_path = report_dir / "starship_first_draft.md"
        output_path.write_text(draft)

        remove_progress_state(report_dir)

        print("\n" + "=" * 80)
        print("GENERATED FIRST DRAFT")
        print("=" * 80)
        print(draft)
        print(f"\nSaved to: {output_path.resolve()}")
    else:
        state["current_section_index"] = len(sections)
        save_progress_state(state)
        print("\nProgress saved. You can continue later and generate when ready.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())