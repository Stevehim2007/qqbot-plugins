"""ABI v1 capability registry with fixed methods and nonblocking readiness.

Providers explicitly hand over a mapping of *fixed* operation names to
callables.  The registry never reflects on an object or accepts a caller-chosen
method name, and binding never waits for a provider to come online.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .contracts import (
    ABI_VERSION,
    ALL_OPERATIONS,
    PROVIDER_METHODS,
    ContractError,
    RequestContext,
    operation_spec,
)

CONTEXT_REGISTRY_ATTR = "_qqbot_abiv1_service_registry"


class RegistryError(ValueError):
    """Stable registry failure for callers and tests."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.message = str(message or code)
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    abi_version: int
    owner_epoch: int
    capabilities: tuple[str, ...]
    methods: Mapping[str, Callable[..., Any]]
    exclusive: bool
    ready: bool
    generation: int

    def advertised(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "abi_version": self.abi_version,
            "owner_epoch": self.owner_epoch,
            "capabilities": list(self.capabilities),
            "ready": self.ready,
            "exclusive": self.exclusive,
        }


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    registry: "ServiceRegistry"
    provider_id: str
    generation: int
    owner_epoch: int
    methods: Mapping[str, Callable[..., Any]]

    def get_method(self, operation: str) -> Callable[..., Any]:
        if not self.registry.is_binding_valid(self):
            raise RegistryError("not_ready", "provider binding is stale")
        spec = operation_spec(operation)
        if spec.provider_id != self.provider_id:
            raise RegistryError("abi_mismatch", "operation owner mismatch")
        method = self.methods.get(operation)
        if not callable(method):
            raise RegistryError("abi_mismatch", "fixed method is not registered")
        return method


def _normalise_capabilities(methods: Mapping[str, Callable[..., Any]]) -> tuple[str, ...]:
    values: list[str] = []
    for operation in methods:
        prefix = str(operation).split(".", 1)[0]
        if prefix and prefix not in values:
            values.append(prefix)
    return tuple(values)


class ServiceRegistry:
    """Thread-safe in-process registry; no waiting and no dynamic reflection."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: dict[str, ProviderDescriptor] = {}
        self._capability_owners: dict[str, list[str]] = {}
        self._capability_epochs: dict[str, int] = {}
        self._generation = 0

    def register_provider(
        self,
        provider_id: str,
        methods: Mapping[str, Callable[..., Any]],
        *,
        capabilities: tuple[str, ...] | list[str] | None = None,
        abi_version: int = ABI_VERSION,
        owner_epoch: int = 0,
        ready: bool = False,
        exclusive: bool = True,
    ) -> ProviderDescriptor:
        provider = str(provider_id or "").strip()
        if provider not in PROVIDER_METHODS:
            raise RegistryError("abi_mismatch", "provider is not in the ABI table")
        if abi_version != ABI_VERSION:
            raise RegistryError("abi_mismatch", "unsupported provider ABI")
        if isinstance(owner_epoch, bool) or not isinstance(owner_epoch, int):
            raise RegistryError("invalid_state", "owner_epoch must be an integer")
        if owner_epoch < 0:
            raise RegistryError("invalid_state", "owner_epoch must be non-negative")
        if not isinstance(methods, Mapping) or not methods:
            raise RegistryError("abi_mismatch", "provider must register fixed methods")
        method_map: dict[str, Callable[..., Any]] = {}
        allowed = set(PROVIDER_METHODS[provider])
        for operation, callable_obj in methods.items():
            operation_name = str(operation)
            if operation_name not in allowed:
                raise RegistryError(
                    "abi_mismatch", "method is not in the provider fixed table"
                )
            if not callable(callable_obj):
                raise RegistryError("abi_mismatch", "fixed method is not callable")
            method_map[operation_name] = callable_obj

        caps = (
            tuple(str(item).strip() for item in capabilities if str(item).strip())
            if capabilities is not None
            else _normalise_capabilities(method_map)
        )
        if not caps or len(set(caps)) != len(caps):
            raise RegistryError("invalid_state", "capabilities must be non-empty and unique")
        for capability in caps:
            if len(capability) > 64 or any(
                ord(char) < 32 or ord(char) == 127 for char in capability
            ):
                raise RegistryError("invalid_state", "capability identifier is invalid")

        with self._lock:
            if provider in self._providers:
                raise RegistryError("duplicate_provider", "provider is already registered")
            for capability in caps:
                existing_owners = self._capability_owners.get(capability, [])
                for owner in existing_owners:
                    existing = self._providers.get(owner)
                    if existing is None:
                        continue
                    if exclusive or existing.exclusive:
                        raise RegistryError(
                            "duplicate_provider", "exclusive capability already owned"
                        )
                current_epoch = self._capability_epochs.get(capability, 0)
                if owner_epoch < current_epoch:
                    raise RegistryError(
                        "owner_epoch_stale", "provider owner epoch is stale"
                    )
            self._generation += 1
            descriptor = ProviderDescriptor(
                provider_id=provider,
                abi_version=abi_version,
                owner_epoch=owner_epoch,
                capabilities=caps,
                methods=MappingProxyType(dict(method_map)),
                exclusive=bool(exclusive),
                ready=bool(ready),
                generation=self._generation,
            )
            self._providers[provider] = descriptor
            for capability in caps:
                self._capability_owners.setdefault(capability, []).append(provider)
                self._capability_epochs[capability] = max(
                    self._capability_epochs.get(capability, 0), owner_epoch
                )
            return descriptor

    # Short alias for consumers/tests that call registry.register(...).
    register = register_provider

    def mark_ready(self, provider_id: str, ready: bool = True) -> ProviderDescriptor:
        provider = str(provider_id or "")
        with self._lock:
            descriptor = self._providers.get(provider)
            if descriptor is None:
                raise RegistryError("not_found", "provider is not registered")
            updated = replace(descriptor, ready=bool(ready))
            self._providers[provider] = updated
            return updated

    def unregister_provider(self, provider_id: str) -> bool:
        provider = str(provider_id or "")
        with self._lock:
            descriptor = self._providers.pop(provider, None)
            if descriptor is None:
                return False
            for capability in descriptor.capabilities:
                owners = [
                    owner
                    for owner in self._capability_owners.get(capability, [])
                    if owner != provider
                ]
                if owners:
                    self._capability_owners[capability] = owners
                else:
                    self._capability_owners.pop(capability, None)
            self._generation += 1
            return True

    # Cleanup-friendly alias.
    unregister = unregister_provider

    def unregister_all(self) -> None:
        with self._lock:
            self._providers.clear()
            self._capability_owners.clear()
            self._generation += 1

    def get_registered_provider(self, provider_id: str) -> ProviderDescriptor | None:
        with self._lock:
            return self._providers.get(str(provider_id or ""))

    # Read-only convenience alias; no provider object/method reflection occurs.
    get_provider = get_registered_provider

    def provider_ids(self) -> tuple[str, ...]:
        """Return registered provider ids without exposing provider internals."""
        with self._lock:
            return tuple(self._providers)

    def has_providers(self) -> bool:
        with self._lock:
            return bool(self._providers)

    def has_ready_providers(self) -> bool:
        with self._lock:
            return any(descriptor.ready for descriptor in self._providers.values())

    def bind_provider(
        self,
        capability: str,
        *,
        owner_epoch: int | None = None,
    ) -> ProviderBinding | None:
        """Return a ready binding or ``None`` immediately; never wait."""

        capability_name = str(capability or "")
        with self._lock:
            for owner in self._capability_owners.get(capability_name, []):
                descriptor = self._providers.get(owner)
                if descriptor is None or not descriptor.ready:
                    continue
                if owner_epoch is not None and owner_epoch != descriptor.owner_epoch:
                    continue
                return ProviderBinding(
                    registry=self,
                    provider_id=descriptor.provider_id,
                    generation=descriptor.generation,
                    owner_epoch=descriptor.owner_epoch,
                    methods=descriptor.methods,
                )
        return None

    bind = bind_provider

    def is_binding_valid(self, binding: ProviderBinding) -> bool:
        with self._lock:
            descriptor = self._providers.get(binding.provider_id)
            return bool(
                descriptor
                and descriptor.ready
                and descriptor.generation == binding.generation
                and descriptor.owner_epoch == binding.owner_epoch
                and dict(descriptor.methods) == dict(binding.methods)
            )

    def resolve_operation(
        self, request_context: RequestContext, operation: str
    ) -> ProviderBinding:
        request_context.validate()
        spec = operation_spec(operation)
        with self._lock:
            descriptor = self._providers.get(spec.provider_id)
            if descriptor is None:
                raise RegistryError("dependency_missing", "provider is not registered")
            if descriptor.abi_version != ABI_VERSION:
                raise RegistryError("abi_mismatch", "provider ABI is incompatible")
            if not descriptor.ready:
                raise RegistryError("not_ready", "provider is not ready")
            current_epoch = 0
            for capability in descriptor.capabilities:
                current_epoch = max(
                    current_epoch, self._capability_epochs.get(capability, 0)
                )
            current_epoch = max(current_epoch, descriptor.owner_epoch)
            if request_context.owner_epoch != current_epoch:
                raise RegistryError(
                    "owner_epoch_stale", "request owner epoch is not current"
                )
            if operation not in descriptor.methods:
                raise RegistryError("abi_mismatch", "operation is not registered")
            return ProviderBinding(
                registry=self,
                provider_id=descriptor.provider_id,
                generation=descriptor.generation,
                owner_epoch=descriptor.owner_epoch,
                methods=descriptor.methods,
            )

    def advertised_capabilities(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            result: dict[str, list[dict[str, Any]]] = {}
            for capability, owners in self._capability_owners.items():
                rows = []
                for owner in owners:
                    descriptor = self._providers.get(owner)
                    if descriptor is None or not descriptor.ready:
                        continue
                    rows.append(
                        {
                            "provider_id": descriptor.provider_id,
                            "abi_version": descriptor.abi_version,
                            "owner_epoch": descriptor.owner_epoch,
                        }
                    )
                if rows:
                    result[capability] = rows
            return result

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                descriptor.advertised()
                for descriptor in sorted(
                    self._providers.values(), key=lambda item: item.provider_id
                )
            ]

    def current_owner_epoch(self, provider_id: str) -> int | None:
        with self._lock:
            descriptor = self._providers.get(str(provider_id or ""))
            if descriptor is None:
                return None
            epochs = [
                self._capability_epochs.get(capability, 0)
                for capability in descriptor.capabilities
            ]
            return max([descriptor.owner_epoch, *epochs], default=0)


def get_or_create_registry(context: Any) -> ServiceRegistry:
    """Attach one registry to an AstrBot Context without blocking startup."""

    existing = getattr(context, CONTEXT_REGISTRY_ATTR, None)
    if isinstance(existing, ServiceRegistry):
        return existing
    registry = ServiceRegistry()
    try:
        setattr(context, CONTEXT_REGISTRY_ATTR, registry)
    except Exception:
        pass
    return registry


def attach_registry(context: Any, registry: ServiceRegistry) -> ServiceRegistry:
    try:
        setattr(context, CONTEXT_REGISTRY_ATTR, registry)
    except Exception:
        pass
    return registry
