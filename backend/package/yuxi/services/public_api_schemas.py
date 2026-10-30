"""Versioned public input types; product AgentRun 1.x stays independent."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextPart(WireModel):
    type: Literal["input_text"]
    text: str = Field(min_length=1, max_length=32768)


class ImagePart(WireModel):
    type: Literal["input_image"]
    image_url: str = Field(min_length=1)


class InputMessage(WireModel):
    type: Literal["message"] = "message"
    role: Literal["user"]
    content: list[Annotated[TextPart | ImagePart, Field(discriminator="type")]] = Field(min_length=1, max_length=18)


class Attachments(WireModel):
    attachment_file_ids: list[Annotated[str, Field(min_length=32, max_length=32)]] = Field(
        default_factory=list, max_length=20
    )


class AgentOverride(WireModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)


class SessionCreate(WireModel):
    agent_id: str = Field(min_length=1, max_length=64)
    agent: AgentOverride | None = None
    input: (
        Annotated[str, Field(min_length=1, max_length=32768)]
        | Annotated[list[InputMessage], Field(min_length=1, max_length=20)]
        | None
    ) = None
    yuxi: Attachments = Field(default_factory=Attachments)
    stream: StrictBool = False
    title: str | None = Field(default=None, max_length=255)
    tool_approval_mode: Literal["always_ask", "always_trust"] | None = None

    @model_validator(mode="after")
    def require_input(self):
        if self.input is None and (self.stream or self.yuxi.attachment_file_ids):
            raise ValueError("流式创建和附件须同时提供 input")
        return self


class SessionOptions(WireModel):
    title: str | None = Field(default=None, max_length=255)
    is_pinned: StrictBool | None = None
    tool_approval_mode: Literal["always_ask", "always_trust"] | None = None


class SessionUpdate(WireModel):
    agent: AgentOverride | None = None
    yuxi: SessionOptions = Field(default_factory=SessionOptions)

    @model_validator(mode="after")
    def reject_null_model(self):
        if self.agent is not None and "model" in self.agent.model_fields_set and self.agent.model is None:
            raise ValueError("agent.model 不能为 null；省略字段以保持当前值")
        return self


class MessageOptions(Attachments):
    mode: Literal["follow_up", "steer"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    tool_approval_mode: Literal["always_ask", "always_trust"] | None = None


class MessageEvent(WireModel):
    type: Literal["agent.session.input.message"]
    input: list[InputMessage] = Field(min_length=1, max_length=20)
    yuxi: MessageOptions = Field(default_factory=MessageOptions)


class OtherAnswer(WireModel):
    type: Literal["other"]
    text: StrictStr = Field(min_length=1)
    selected: list[StrictStr]


class AnswerItem(WireModel):
    question_id: str = Field(min_length=1)
    answer: StrictStr | Annotated[list[StrictStr], Field(min_length=1)] | OtherAnswer


class AnswerPayload(WireModel):
    type: Literal["answer"]
    answers: list[AnswerItem] = Field(min_length=1, max_length=20)


class ApprovalDecision(WireModel):
    call_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]


class ApprovalPayload(WireModel):
    type: Literal["approval"]
    decisions: list[ApprovalDecision] = Field(min_length=1, max_length=20)


class ResumeEvent(WireModel):
    type: Literal["yuxi.session.input.resume"]
    turn_id: str = Field(min_length=1, max_length=64)
    waitpoint_id: str = Field(min_length=1, max_length=128)
    response: Annotated[AnswerPayload | ApprovalPayload, Field(discriminator="type")]


class CancelOptions(WireModel):
    turn_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_run_id: str | None = Field(default=None, min_length=1, max_length=64)


class CancelEvent(WireModel):
    type: Literal["agent.session.input.cancel"]
    yuxi: CancelOptions = Field(default_factory=CancelOptions)


class ContinueEvent(WireModel):
    type: Literal["yuxi.session.input.continue"]


class PromoteEvent(WireModel):
    type: Literal["yuxi.session.input.promote"]
    input_id: str = Field(min_length=1, max_length=64)


class CancelInputEvent(WireModel):
    type: Literal["yuxi.session.input.cancel_input"]
    input_id: str = Field(min_length=1, max_length=64)


class TreeEvent(WireModel):
    type: Literal["yuxi.session.tree.stop", "yuxi.session.tree.continue"]


Event = Annotated[
    MessageEvent | ResumeEvent | CancelEvent | ContinueEvent | PromoteEvent | CancelInputEvent | TreeEvent,
    Field(discriminator="type"),
]


class SessionEventCreate(WireModel):
    events: list[Event] = Field(min_length=1, max_length=1)
