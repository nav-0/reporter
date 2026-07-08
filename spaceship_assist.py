import base64
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import openai
from pypdf import PdfReader
from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.widgets import Box, Frame, Label, TextArea


TEMPLATE = """# Starship Specification

## Vessel Overview

Describe the starship, its purpose, mission profile, and operating context.

## Propulsion System

Describe the propulsion model, energy source, engine design, and known constraints.

## Crew and Passenger Model

Describe who uses the vessel, who operates it, and what access or responsibility each group has.

## Cargo and Alien Artifacts

Describe the types of cargo, unusual materials, alien objects, and handling requirements.

## Emergency Pancake Protocol

Describe emergency procedures involving breakfast systems, pancake containment, syrup routing, and crew safety.
"""


def ask_copilot(prompt: str, image_paths: list = None) -> str:
    """Call Copilot API with optional vision support for images."""
    if image_paths is None:
        image_paths = []
    
    client = openai.OpenAI(
        base_url="https://api.githubcopilot.com",
        api_key=os.environ.get("GITHUB_TOKEN")
    )
    
    content = [{"type": "text", "text": prompt}]
    
    for path in image_paths:
        if Path(path).exists():
            image_data = base64.b64encode(Path(path).read_bytes()).decode()
            suffix = Path(path).suffix.lower()
            media_type = "image/png" if suffix == ".png" else "image/jpeg"
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{image_data}"}
            })
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": content}]
    )
    
    return response.choices[0].message.content


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
    width = 20
    filled = int(width * current / total)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {current}/{total}"


def is_image(path: str):
    return Path(path).suffix.lower() in [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"]


def is_pdf(path: str):
    return Path(path).suffix.lower() == ".pdf"


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
        lines.append("Files:")
        for item in section_evidence["files"]:
            lines.append(f"- {item}")

    if section_evidence["document_images"]:
        lines.append("Images to include in final draft:")
        for item in section_evidence["document_images"]:
            lines.append(f"- {item}")

    if section_evidence["context_images"]:
        lines.append("Context-only images:")
        for item in section_evidence["context_images"]:
            lines.append(f"- {item}")

    if not lines:
        return "(none yet)"

    return "\n".join(lines)


def take_screenshot(section_title: str, evidence, evidence_area):
    assets_dir = Path("assets")
    assets_dir.mkdir(exist_ok=True)

    count = len(evidence["document_images"]) + len(evidence["context_images"]) + 1
    filename = assets_dir / f"{slugify(section_title)}-{count}.png"

    subprocess.run(["screencapture", "-i", str(filename)])

    mode = input("\nUse this screenshot in final draft? [y = include / n = context only]: ").strip().lower()

    if mode == "y":
        evidence["document_images"].append(str(filename))
    else:
        evidence["context_images"].append(str(filename))

    evidence_area.text = render_evidence(evidence)


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

    header = Label(
        text=f"Section {section_number} of {total_sections}: {section['title']}    "
             f"{progress_bar(section_number - 1, total_sections)}"
    )

    command_footer = Label(
        text="Commands: Ctrl-N = next section | Ctrl-S = screenshot | Ctrl-C = quit"
    )

    @kb.add("c-n")
    def _(event):
        event.app.exit(result=input_area.text)

    @kb.add("c-s")
    def _(event):
        run_in_terminal(
            lambda: take_screenshot(section["title"], evidence, evidence_area)
        )

    @kb.add("c-c")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt)

    left = HSplit([
        Frame(input_area, title="Data Entry - type notes, paste text, or drag/drop file paths here"),
        Frame(evidence_area, title="Current Evidence")
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


def ask_image_usage(path: str, evidence):
    mode = input(f"\nImage added: {path}\nUse in final draft? [y = include / n = context only]: ").strip().lower()

    if mode == "y":
        evidence["document_images"].append(path)
    else:
        evidence["context_images"].append(path)


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
    """Build text prompt for Copilot. Images will be passed separately."""
    prompt_parts = []

    prompt_parts.append("Generate a clean markdown first draft using the template and evidence below.")
    prompt_parts.append("Use only the supplied evidence. Do not invent missing details.")
    prompt_parts.append("If a section has little or no evidence, keep it simple and do not fabricate.")
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

        if evidence["context_images"]:
            prompt_parts.append("\nCONTEXT-ONLY REFERENCE MATERIALS: See attached images.\n")

    prompt_parts.append(
        "\n\nReturn only the completed markdown draft and nothing else. "
        "Do not include explanations, notes, logs, metadata, code fences, or any surrounding text."
    )

    return "\n".join(prompt_parts)


def collect_all_context_images(all_evidence):
    """Gather all context_images from all sections for vision API."""
    images = []
    for evidence in all_evidence.values():
        images.extend(evidence.get("context_images", []))
    return images


def main():
    if not sys.stdin.isatty():
        print("This program requires an interactive terminal (TTY) for the full-screen UI.")
        print("Run it directly in a real terminal, not in a piped/scripted/non-interactive context.")
        return 1

    sections = parse_sections(TEMPLATE)

    all_evidence = {
        section["title"]: {
            "text": [],
            "files": [],
            "document_images": [],
            "context_images": []
        }
        for section in sections
    }

    for index, section in enumerate(sections, start=1):
        title = section["title"]
        evidence = all_evidence[title]

        raw_input_text = collect_section(
            section=section,
            section_number=index,
            total_sections=len(sections),
            evidence=evidence
        )

        text, files = parse_input_into_text_and_files(raw_input_text)

        if text:
            evidence["text"].append(text)

        for path in files:
            if is_image(path):
                ask_image_usage(path, evidence)
            else:
                evidence["files"].append(path)

    print_bill_of_evidence(sections, all_evidence)

    generate = input("\nGenerate first draft with Copilot? [y/n]: ").strip().lower()

    if generate == "y":
        prompt = build_generation_prompt(sections, all_evidence)
        context_images = collect_all_context_images(all_evidence)
        
        draft = ask_copilot(prompt, image_paths=context_images)
        draft = extract_markdown_output(draft)

        output_path = Path("starship_first_draft.md")
        output_path.write_text(draft)

        print("\n" + "=" * 80)
        print("GENERATED FIRST DRAFT")
        print("=" * 80)
        print(draft)
        print(f"\nSaved to: {output_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())