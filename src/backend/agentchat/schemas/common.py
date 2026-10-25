from typing import List, Optional, Literal
from pydantic import BaseModel, Field, model_validator


class ModelConfig(BaseModel):
    model_name: str = ""
    api_key: str = ""
    base_url: str = ""

class MultiModels(BaseModel):
    class Config:
        # 允许从dict额外属性创建模型
        extra = "allow"

    reasoning_model: ModelConfig = Field(default_factory=ModelConfig)
    conversation_model: ModelConfig = Field(default_factory=ModelConfig)
    tool_call_model: ModelConfig = Field(default_factory=ModelConfig)
    qwen3_coder: ModelConfig = Field(default_factory=ModelConfig)
    qwen_vl: ModelConfig = Field(default_factory=ModelConfig)
    text2image: ModelConfig = Field(default_factory=ModelConfig)
    embedding: ModelConfig = Field(default_factory=ModelConfig)
    rerank: ModelConfig = Field(default_factory=ModelConfig)

class Tools(BaseModel):
    class Config:
        extra = "allow"

    weather: dict = Field(default_factory=dict)
    tavily: dict = Field(default_factory=dict)
    google: dict = Field(default_factory=dict)
    delivery: dict = Field(default_factory=dict)
    bocha: dict = Field(default_factory=dict)


class Rag(BaseModel):
    class Config:
        extra = "allow"

    enable_elasticsearch: bool = Field(default=False)
    enable_summary: bool = Field(default=False)
    enable_ik_analyzer: bool = Field(default=False)
    # 历史拼写（少一个 e），代码各处按此读取：app_settings.rag.retrival
    # 正确拼写 retrieval 由下方 _accept_retrieval_alias 归一化到本字段，
    # 因此两种写法都能生效。
    retrival: dict = Field(default_factory=dict)
    split: dict = Field(default_factory=dict)
    elasticsearch: dict = Field(default_factory=dict)
    vector_db: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_retrieval_alias(cls, data):
        """同时接受 retrival（历史拼写）与 retrieval（正确拼写）。

        为什么需要：配置里一直写的是 retrival，代码也读 retrival。
        若有人按正确拼写写 retrieval，pydantic 会把该键当作额外字段（本模型
        extra="allow"），retrival 仍为空 dict —— 于是 top_k / min_score 静默
        取不到，RAG 检索悄悄退化到默认行为，且没有任何报错。
        这里在解析前把 retrieval 归一化到 retrival，两种写法都能生效。

        两者同时出现时以「正确拼写」为准，历史拼写仅补充其缺失的键。
        """
        if not isinstance(data, dict):
            return data

        data = dict(data)
        if "retrieval" in data:
            correct = data.pop("retrieval")
            legacy = data.get("retrival")

            if isinstance(correct, dict) and isinstance(legacy, dict):
                data["retrival"] = {**legacy, **correct}
            elif correct is not None:
                data["retrival"] = correct
        return data



class OSSConfig(BaseModel):
    access_key_id: str
    access_key_secret: str
    endpoint: str
    bucket_name: str
    base_url: str


class MinioConfig(BaseModel):
    access_key_id: str
    access_key_secret: str
    endpoint: str
    bucket_name: str
    base_url: str

class StorageConfig(BaseModel):
    mode: Literal["oss", "minio"]
    oss: Optional[OSSConfig] = None
    minio: Optional[MinioConfig] = None

    @model_validator(mode="after")
    def validate_storage(self):
        if self.mode == "oss" and not self.oss:
            raise ValueError("mode=oss 时必须提供 aliyun_oss")
        if self.mode == "minio" and not self.minio:
            raise ValueError("mode=minio 时必须提供 minio")
        return self

    @property
    def active(self):
        return self.oss if self.mode == "oss" else self.minio

class ServerConfig(BaseModel):
    name: str = "AgentChat"
    version: str = "2.5.0"
    host: str = "127.0.0.1"
    port: int = 7860
    env: str = "dev"