"""Built-in manifest validator providers."""

from chart_manager.services.manifest_validation.validator_adapters import (
    KubeconformProvider,
    KyvernoProvider,
)
from chart_manager.services.manifest_validation.validators import (
    ValidatorProvider,
    validate_registry,
)

_PROVIDERS: tuple[ValidatorProvider, ...] = (
    KubeconformProvider(),
    KyvernoProvider(),
)
VALIDATOR_REGISTRY = validate_registry(_PROVIDERS)
