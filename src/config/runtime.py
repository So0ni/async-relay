"""Runtime configuration manager with hash-based validation."""

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from src.config.loader import load_config
from src.config.models import Config

logger = logging.getLogger(__name__)


class RuntimeConfigManager:
    """
    Manages runtime configuration with hash-based validation.

    - Loads from config.yaml at startup
    - Compares hash between config.yaml and config.runtime.yaml
    - If hashes don't match, config.yaml takes precedence
    - UI modifications write to config.runtime.yaml
    """

    def __init__(self, config_path: str):
        """
        Initialize runtime config manager.

        Args:
            config_path: Path to main config file (config.yaml)
        """
        self.config_path = Path(config_path).resolve()
        self.runtime_path = self.config_path.parent / "config.runtime.yaml"

        logger.info("Runtime config manager initialized")
        logger.info(f"  Config file: {self.config_path}")
        logger.info(f"  Runtime file: {self.runtime_path}")

    def _compute_hash(self, path: Path) -> str | None:
        """
        Compute SHA256 hash of a file.

        Args:
            path: Path to file

        Returns:
            Hex digest of file hash, or None if file doesn't exist
        """
        if not path.exists():
            return None

        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            logger.error(f"Failed to compute hash for {path}: {e}")
            return None

    def _load_yaml(self, path: Path) -> dict[str, Any] | None:
        """
        Load YAML file.

        Args:
            path: Path to YAML file

        Returns:
            Parsed YAML data, or None if file doesn't exist or is invalid
        """
        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = yaml.safe_load(f)
                return data if data else {}
        except Exception as e:
            logger.error(f"Failed to load YAML from {path}: {e}")
            return None

    def _save_yaml(self, path: Path, data: dict[str, Any]) -> bool:
        """
        Save data to YAML file.

        Args:
            path: Path to YAML file
            data: Data to save

        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure parent directory exists
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

            logger.info(f"Saved configuration to {path}")
            return True

        except Exception as e:
            logger.error(f"Failed to save YAML to {path}: {e}")
            return False

    def load_active_config(self) -> Config:
        """
        Load active configuration with hash validation.

        Logic:
        1. Compute hash of config.yaml
        2. Load config.runtime.yaml if exists
        3. Check if runtime has matching base_config_hash
        4. If hashes don't match or runtime doesn't exist:
           - Load from config.yaml
           - Create/overwrite config.runtime.yaml
        5. If hashes match:
           - Load from config.runtime.yaml

        Returns:
            Active configuration
        """
        # Always compute config.yaml hash
        config_hash = self._compute_hash(self.config_path)
        if not config_hash:
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        # Try to load runtime config
        runtime_data = self._load_yaml(self.runtime_path)

        # Check hash match
        use_runtime = False
        if runtime_data:
            stored_hash = runtime_data.get("_meta", {}).get("base_config_hash")
            if stored_hash == config_hash:
                use_runtime = True
                logger.info("Hash match: using runtime configuration")
            else:
                logger.warning(
                    "Hash mismatch: config.yaml has changed, overwriting runtime configuration"
                )
        else:
            logger.info("No runtime config found, using config.yaml")

        # Load from config.yaml and create runtime file
        if not use_runtime:
            config = load_config(self.config_path)
            self._create_runtime_from_config(config_hash)
            return config

        # Load from runtime file
        try:
            # Remove metadata before loading as Config
            # mypy: runtime_data is guaranteed to be dict here due to use_runtime check
            assert runtime_data is not None
            runtime_data_copy = dict(runtime_data)
            runtime_data_copy.pop("_meta", None)

            # Save to temp file and load using existing loader
            temp_path = self.runtime_path.parent / ".config.runtime.tmp"
            self._save_yaml(temp_path, runtime_data_copy)

            config = load_config(temp_path)
            temp_path.unlink()  # Clean up temp file

            logger.info("Loaded configuration from runtime file")
            return config

        except Exception as e:
            logger.error(f"Failed to load runtime config: {e}")
            logger.info("Falling back to config.yaml")
            config = load_config(self.config_path)
            self._create_runtime_from_config(config_hash)
            return config

    def _create_runtime_from_config(self, config_hash: str) -> None:
        """
        Create runtime config file from main config file.

        Args:
            config_hash: Hash of config.yaml
        """
        config_data = self._load_yaml(self.config_path)
        if not config_data:
            logger.error("Failed to load config.yaml for runtime creation")
            return

        # Add metadata
        runtime_data = {
            "_meta": {
                "base_config_hash": config_hash,
                "source": "config.yaml",
            },
            **config_data,
        }

        self._save_yaml(self.runtime_path, runtime_data)
        logger.info("Created runtime configuration from config.yaml")

    def save_runtime_config(self, config_dict: dict[str, Any]) -> bool:
        """
        Save runtime configuration from UI modifications.

        Args:
            config_dict: Configuration dictionary to save

        Returns:
            True if successful, False otherwise
        """
        # Compute current config.yaml hash
        config_hash = self._compute_hash(self.config_path)
        if not config_hash:
            logger.error("Cannot save runtime config: config.yaml not found")
            return False

        # Add metadata
        runtime_data = {
            "_meta": {
                "base_config_hash": config_hash,
                "source": "ui_modified",
            },
            **config_dict,
        }

        return self._save_yaml(self.runtime_path, runtime_data)

    def get_config_dict(self) -> dict[str, Any]:
        """
        Get current runtime configuration as dictionary.

        Returns:
            Configuration dictionary without metadata
        """
        runtime_data = self._load_yaml(self.runtime_path)
        if not runtime_data:
            runtime_data = self._load_yaml(self.config_path)

        if not runtime_data:
            return {}

        # Remove metadata
        config_data = dict(runtime_data)
        config_data.pop("_meta", None)
        return config_data

    def get_config_source(self) -> str:
        """
        Get source of current active configuration.

        Returns:
            'config.yaml' or 'ui_modified'
        """
        runtime_data = self._load_yaml(self.runtime_path)
        if not runtime_data:
            return "config.yaml"

        source: str = runtime_data.get("_meta", {}).get("source", "config.yaml")
        return source
