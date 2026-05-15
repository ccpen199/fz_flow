import os
from typing import Any, Optional

from llmai import get_client
from llmai.shared import JSONSchemaResponse, Message, SystemMessage, UserMessage
from models.presentation_layout import PresentationLayoutModel
from models.presentation_outline_model import PresentationOutlineModel
from utils.llm_config import get_llm_config
from utils.llm_client_error_handler import handle_llm_client_exceptions
from utils.llm_utils import generate_structured_with_schema_retries
from utils.llm_provider import get_model
from utils.get_dynamic_models import get_presentation_structure_model_with_n_slides
from utils.schema_utils import ensure_array_schemas_have_items
from models.presentation_structure_model import PresentationStructureModel


STRUCTURE_FROM_SLIDES_MARKDOWN_SYSTEM_PROMPT = """
You will be given available slide layouts and content for each slide.
You need to select a layout for each slide based on the mentioned guidelines.

# Steps
1. Analyze all available slide layouts.
2. Analyze content for each slide.
3. Select a layout for each slide one by one by following the selection rules.

# Analyzing Slide Layouts
- Identify what each layout contains based on provided schema markdown.

# Analyzing Content
- Identify how the content is structured.
- Identify if the content contains tables.

# Selection Rules
- If content contains table, then select either table layout or graph layout.
- Don't select layout with image unless content contains image.
- Don't select table layout if content does not contain table.
- You are allowed to select same layout for multiple slides.

# Table Layout Selection Rules
- Must select table layout if the content contains table with text data.
- Must only select a layout with table if the table only contains text data.

# Graph Layout Selection Rules
- Must only select a layout with chart if the content contains table with numeric data.
- Identify how many columns are present in the table.
- Must select a layout that supports n-1 charts for n columns.
- Must prioritize layouts that support multiple charts.
- Don't select metrics layout for content containing table with numeric data.
- For example, if content contains table with 3 columns, then select a layout that supports 2 charts.

{user_instructions}

# Output Rules: 
- One layout index for each slide.
- Example: [0, 1, 2, 3, 4]

{presentation_layout}
"""


GET_MESSAGES_SYSTEM_PROMPT = """
You're a professional presentation designer with creative freedom to design engaging presentations.

# DESIGN PHILOSOPHY
- Create visually compelling and varied presentations
- Match layout to content purpose and audience needs

# Layout Selection Guidelines
1. **Content-driven choices**: Let the slide's purpose guide layout selection
- Opening/closing → Title layouts
- Processes/workflows → Visual process layouts  
- Comparisons/contrasts → Side-by-side layouts
- Data/metrics → Chart/graph layouts
- Concepts/ideas → Image + text layouts
- Key insights → Emphasis layouts

2. **Visual variety**: Aim for diverse slide layouts across the presentation. 
- Don't use same layout for multiple slides unless necessary.
- Mix text-heavy and visual-heavy slides naturally
- Use your judgment on when repetition serves the content
- Balance information density across slides
- Adjacent slide layouts should be different unless instructed/necessary otherwise.

3. **Audience experience**: Consider how slides work together
- Create natural transitions between topics

4. **Table of contents**:
- Must only use table of contents layout if slide content contains table of contents.

{user_instruction_header}

User instruction should be taken into account while creating the presentation structure, except for number of slides.

Select layout index for each of the {n_slides} slides based on what will best serve the presentation's goals.

"""


def get_messages(
    presentation_layout: PresentationLayoutModel,
    n_slides: int,
    data: str,
    instructions: Optional[str] = None,
) -> list[Message]:
    system_prompt = GET_MESSAGES_SYSTEM_PROMPT.format(
        user_instruction_header="# User Instruction:" if instructions else "",
        n_slides=n_slides,
    )

    return [
        SystemMessage(content=system_prompt),
        UserMessage(
            content=(
                f"{presentation_layout.to_string()}\n\n"
                "--------------------------------------\n\n"
                f"{data}"
            )
        ),
    ]


def get_messages_for_slides_markdown(
    presentation_layout: PresentationLayoutModel,
    n_slides: int,
    data: str,
    instructions: Optional[str] = None,
) -> list[Message]:
    system_prompt = STRUCTURE_FROM_SLIDES_MARKDOWN_SYSTEM_PROMPT.format(
        user_instructions=instructions or "",
        presentation_layout=presentation_layout.to_string(with_schema=True),
    )

    return [SystemMessage(content=system_prompt), UserMessage(content=data)]


def _coerce_layout_index(value: Any, max_layout_index: int) -> int:
    if isinstance(value, dict):
        for key in ("slide_layout", "slideLayout", "layout", "layout_index", "layoutIndex", "index"):
            if key in value:
                value = value[key]
                break
        else:
            value = 0
    try:
        index = int(value)
    except (TypeError, ValueError):
        index = 0
    return max(0, min(index, max_layout_index))


def _normalize_structure_content(content: dict, presentation_layout: PresentationLayoutModel) -> dict:
    max_layout_index = max(0, len(presentation_layout.slides) - 1)
    slides = content.get("slides")

    if slides is None:
        for key in ("slide_layouts", "slideLayouts", "layouts"):
            if isinstance(content.get(key), list):
                slides = content[key]
                break

    if slides is None:
        for key in ("slide_layout", "slideLayout", "layout", "layout_index", "layoutIndex"):
            if key in content:
                slides = [content[key]]
                break

    if not isinstance(slides, list):
        return content

    return {"slides": [_coerce_layout_index(item, max_layout_index) for item in slides]}


def _layout_directive(content: str) -> str | None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("PRESENTON_LAYOUT_ID:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _directed_structure(
    presentation_outline: PresentationOutlineModel,
    presentation_layout: PresentationLayoutModel,
) -> PresentationStructureModel | None:
    layout_indexes: list[int] = []
    for slide in presentation_outline.slides:
        layout_id = _layout_directive(slide.content)
        if not layout_id:
            return None
        layout_indexes.append(presentation_layout.get_slide_layout_index(layout_id))
    return PresentationStructureModel(slides=layout_indexes)


def _forced_structure(
    presentation_layout: PresentationLayoutModel,
    n_slides: int,
) -> PresentationStructureModel | None:
    forced_layout_id = os.getenv("PRESENTON_FORCE_LAYOUT_ID", "").strip()
    if not forced_layout_id:
        return None

    layout_index = presentation_layout.get_slide_layout_index(forced_layout_id)
    return PresentationStructureModel(slides=[layout_index for _ in range(n_slides)])


async def generate_presentation_structure(
    presentation_outline: PresentationOutlineModel,
    presentation_layout: PresentationLayoutModel,
    instructions: Optional[str] = None,
    using_slides_markdown: bool = False,
) -> PresentationStructureModel:
    directed_structure = _directed_structure(presentation_outline, presentation_layout)
    if directed_structure is not None:
        return directed_structure

    forced_structure = _forced_structure(
        presentation_layout,
        len(presentation_outline.slides),
    )
    if forced_structure is not None:
        return forced_structure

    client = get_client(config=get_llm_config())
    model = get_model()
    response_model = get_presentation_structure_model_with_n_slides(
        len(presentation_outline.slides)
    )

    try:
        messages = (
            get_messages_for_slides_markdown(
                presentation_layout,
                len(presentation_outline.slides),
                presentation_outline.to_string(),
                instructions,
            )
            if using_slides_markdown
            else get_messages(
                presentation_layout,
                len(presentation_outline.slides),
                presentation_outline.to_string(),
                instructions,
            )
        )
        structure_schema = ensure_array_schemas_have_items(response_model.model_json_schema())
        response_format = JSONSchemaResponse(
            name="response",
            json_schema=structure_schema,
            strict=True,
        )

        content = await generate_structured_with_schema_retries(
            client,
            model,
            messages=messages,
            response_format=response_format,
            json_schema=structure_schema,
            strict=True,
            validate_schema=False,
        )
        content = _normalize_structure_content(content, presentation_layout)
        return PresentationStructureModel(**content)
    except Exception as e:
        raise handle_llm_client_exceptions(e)
