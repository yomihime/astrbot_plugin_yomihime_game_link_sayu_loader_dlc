"""Plugin configuration values and atomic storage inside a single worker's data root."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tempfile


@dataclass
class GsConfig:
    title: str
    desc: str


@dataclass
class GsBoolConfig(GsConfig):
    data: bool
    secret: bool = False


@dataclass
class GsStrConfig(GsConfig):
    data: str
    options: list = field(default_factory=list)
    regex: str | None = None
    details: dict | None = None
    secret: bool = False


@dataclass
class GsIntConfig(GsConfig):
    data: int
    max_value: int | None = None
    options: list = field(default_factory=list)
    secret: bool = False


@dataclass
class GsFloatConfig(GsConfig):
    data: float
    min_value: float | None = None
    max_value: float | None = None
    secret: bool = False


@dataclass
class GsListStrConfig(GsConfig):
    data: list
    options: list = field(default_factory=list)
    secret: bool = False


@dataclass
class GsDictConfig(GsConfig):
    data: dict
    secret: bool = False


@dataclass
class GsImageConfig(GsConfig):
    data: str
    upload_to: str
    filename: str
    suffix: str = "jpg"
    secret: bool = False


MODELS = {
    cls.__name__: cls
    for cls in (
        GsBoolConfig,
        GsStrConfig,
        GsIntConfig,
        GsFloatConfig,
        GsListStrConfig,
        GsDictConfig,
        GsImageConfig,
    )
}


def configuration_type(root):
    instances = {}

    def checked(path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise ValueError("configuration path outside plugin data")
        return resolved

    class StringConfig:
        def __new__(cls, config_name, CONFIG_PATH, config_list):
            if config_name is None:
                raise ValueError("Config.name is None!")
            if config_name not in instances:
                instances[config_name] = super().__new__(cls)
            return instances[config_name]

        def __init__(self, config_name, CONFIG_PATH, config_list):
            paths = CONFIG_PATH if isinstance(CONFIG_PATH, list) else [CONFIG_PATH]
            *previous, self.CONFIG_PATH = map(checked, paths)
            self.config_name = config_name
            self.config_list = self.config_default = config_list
            for old in previous:
                if old.is_file() and not self.CONFIG_PATH.exists():
                    self.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    old.replace(self.CONFIG_PATH)
                    break
            self.update_config()

        def update_config(self):
            defaults = deepcopy(self.config_list)
            if self.CONFIG_PATH.exists():
                saved = json.loads(self.CONFIG_PATH.read_bytes())
                for key, row in saved.items():
                    if not isinstance(row, dict) or row.get("type") not in MODELS:
                        if key not in defaults:
                            raise ValueError("unsupported stored config type")
                        continue
                    cls = MODELS[row["type"]]
                    if key not in defaults:
                        defaults[key] = cls(
                            **{k: v for k, v in row.items() if k != "type"}
                        )
                    elif type(defaults[key]) is cls and type(row.get("data")) is type(
                        defaults[key].data
                    ):
                        defaults[key].data = row["data"]
            self.config = defaults
            self.write_config()

        def __iter__(self):
            return iter(self.config)

        def __len__(self):
            return len(self.config)

        def __getitem__(self, key):
            return self.config[key]

        def get_raw_config(self):
            return {
                k: {"type": type(v).__name__, **asdict(v)}
                for k, v in self.config.items()
            }

        def write_config(self):
            path = checked(self.CONFIG_PATH)
            payload = json.dumps(self.get_raw_config(), ensure_ascii=False, indent=4)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                pending = Path(stream.name)
            try:
                pending.write_text(payload, encoding="utf-8")
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)

        def get_config(self, key, default_value=None):
            if key in self.config:
                return self.config[key]
            cls = {
                str: GsStrConfig,
                bool: GsBoolConfig,
                list: GsListStrConfig,
                dict: GsDictConfig,
            }.get(type(default_value))
            if cls is None:
                cls, default_value = GsBoolConfig, False
            return cls("缺省值", "获取错误的配置项", default_value)

        def set_config(self, key, value):
            if key not in self.config_list:
                return False
            setting = self.config[key]
            if isinstance(setting, GsFloatConfig) and type(value) is int:
                value = float(value)
            if type(value) is not type(setting.data):
                return False
            old, setting.data = setting.data, value
            try:
                self.write_config()
            except BaseException:
                setting.data = old
                raise
            return True

    return StringConfig
