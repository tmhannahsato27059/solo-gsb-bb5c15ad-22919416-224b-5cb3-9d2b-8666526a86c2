"""请求 / 响应数据模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SegmentInput(BaseModel):
    """一条线段：电流自父节点流向子节点。"""

    id: Optional[str] = Field(default=None, description="线段标识，缺省自动生成 seg-<序号>")
    parent: str = Field(min_length=1, description="父节点名称，根节点固定为 source")
    child: str = Field(min_length=1, description="子节点名称")
    resistance: float = Field(ge=0, description="线段电阻 (Ω)，不允许为负")
    rated_current: float = Field(gt=0, description="线段额定电流 (A)，必须为正")


class LoadInput(BaseModel):
    """节点恒定负载。"""

    node: str = Field(min_length=1, description="节点名称")
    current: float = Field(ge=0, description="恒定负载电流 (A)，不允许为负")
    min_voltage: Optional[float] = Field(
        default=None, ge=0, description="该节点最低允许电压 (V)，缺省使用全局 min_voltage"
    )


class EvaluateRequest(BaseModel):
    source_voltage: float = Field(gt=0, description="电源电压 (V)，必须为正")
    min_voltage: float = Field(ge=0, description="全局最低允许电压 (V)")
    segments: list[SegmentInput] = Field(
        min_length=1, description="线段列表，须构成以 source 为根的连通树"
    )
    loads: list[LoadInput] = Field(default_factory=list, description="各节点恒定负载电流")


class SegmentResult(BaseModel):
    segment_id: str
    parent: str
    child: str
    current: float = Field(description="线段承载的全部下游电流 (A)")
    rated_current: float = Field(description="线段额定电流 (A)")
    resistance: float = Field(description="线段电阻 (Ω)")
    voltage_drop: float = Field(description="线段压降 (V)")
    over_rated: bool = Field(description="是否超过额定电流")


class NodeResult(BaseModel):
    node: str
    voltage: float = Field(description="节点电压 (V)")
    min_voltage: float = Field(description="该节点生效的最低允许电压 (V)")
    load_current: float = Field(description="节点恒定负载电流 (A)")
    under_voltage: bool = Field(description="是否欠压")


class OverCurrentViolation(BaseModel):
    segment_id: str
    parent: str
    child: str
    current: float
    rated_current: float
    excess: float = Field(description="超出额定值的电流 (A)")


class UnderVoltageViolation(BaseModel):
    node: str
    voltage: float
    min_voltage: float
    deficit: float = Field(description="低于最低允许电压的差值 (V)")


class EvaluateResponse(BaseModel):
    ok: bool = Field(description="无超额定且无欠压时为 true")
    source_voltage: float
    min_voltage: float
    total_source_current: float = Field(description="电源输出的总电流 (A)")
    segments: list[SegmentResult]
    nodes: list[NodeResult]
    over_current_segments: list[OverCurrentViolation] = Field(description="超额定电流的线段")
    under_voltage_nodes: list[UnderVoltageViolation] = Field(description="欠压节点")


class CapacityRequest(EvaluateRequest):
    """增容评估请求：在 /evaluate 参数基础上指定目标节点。"""

    target_node: str = Field(min_length=1, description="目标节点名称（须为已有的非 source 节点）")


class LimitingFactor(BaseModel):
    type: str = Field(description="限制类型：segment=线段额定电流，node=节点最低允许电压")
    id: str = Field(description="达到上限的线段 id 或节点名称")


class CapacityResponse(BaseModel):
    target_node: str = Field(description="目标节点名称")
    max_additional_current: float = Field(
        description="目标节点可增加的最大恒定负载电流 (A)，向下截断至 6 位小数"
    )
    limiting_factors: list[LimitingFactor] = Field(description="达到上限的线段 / 节点")
