"""HTTP 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, PositiveInt


class ReleaseOut(BaseModel):
    id: int = Field(..., description="叶子下标 = id - 1")
    station_id: str
    sample_date: str
    order_no: str
    blob_sha256: str
    size: int
    created_at: str


class PublishResult(BaseModel):
    reused: bool = Field(..., description="True 表示同单同输入重试，复用既有记录")
    release: ReleaseOut
    checkpoint: "CheckpointOut"


class CheckpointOut(BaseModel):
    seq: int = Field(..., description="截至该检查点的发布条数")
    root_hex: str
    created_at: str


class LeafRecordOut(BaseModel):
    v: int
    station_id: str
    sample_date: str
    order_no: str
    blob_sha256: str
    size: int


class InclusionProofOut(BaseModel):
    index: int
    checkpoint_seq: int
    checkpoint_root: str
    tree_depth: int
    leaf_record: LeafRecordOut
    leaf_hash: str
    path: list[dict]


class AppendProofOut(BaseModel):
    old_seq: int
    new_seq: int
    old_root: str
    new_root: str
    tree_depth: int
    entries: list[dict] = Field(
        ..., description="完美子树条目 (start, height, hash)，数量 O(log n)"
    )


class HealthOut(BaseModel):
    status: str
    release_count: int
    problems: list[str]


class StatPackageCreateIn(BaseModel):
    release_ids: list[PositiveInt] = Field(
        ...,
        min_length=1,
        description="参与制包的发布序号列表；重复序号自动去重，同一序号只计一次",
    )


class StatPackageMergeIn(BaseModel):
    package_ids: list[PositiveInt] = Field(
        ...,
        min_length=1,
        description="待合并的包号列表；重复包号自动去重，跨包同一发布序号只计一次",
    )


class StatPackageOut(BaseModel):
    package_id: int = Field(..., description="包号（1 起稠密，跨实例与重启稳定）")
    release_ids: list[int] = Field(..., description="包内所含发布序号（升序、已去重）")
    row_count: int = Field(..., description="两列均有值的有效行数合计")
    pearson_r: float | None = Field(
        ...,
        description="pH 与电导率的 Pearson 相关系数；样本不足两行或任一列恒定时为 null",
    )
    created_at: str


PublishResult.model_rebuild()
