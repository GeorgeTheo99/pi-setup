"""Composable model access; legacy modes remain topology shortcuts."""
CHOICES = ("direct", "model-gateway", "existing-gateway", "omlx")
MODES = {"direct": {"direct"}, "cloud": {"model-gateway"},
         "local": {"model-gateway", "omlx"}, "both": {"model-gateway", "omlx"},
         "later": set(), "existing-gateway": {"existing-gateway"}}


def saved(receipt):
    if not receipt:
        return set()
    if "model_access" in receipt:
        return set(receipt["model_access"])
    result = set(MODES.get(receipt.get("mode"), ()))
    if "model-gateway" in receipt.get("modules", ()):
        result.add("model-gateway")
    if receipt.get("external_gateway"):
        result.add("existing-gateway")
    if receipt.get("omlx"):
        result.update(("omlx", "model-gateway"))
    return result


def resolve(mode, additions, previous):
    selection = set(MODES[mode])
    if additions:
        selection.update(saved(previous))
        selection.update(additions)
    if selection - set(CHOICES):
        raise RuntimeError("Unknown model-access capability")
    if "omlx" in selection:
        selection.add("model-gateway")
    if {"model-gateway", "existing-gateway"} <= selection:
        raise RuntimeError("Local and external gateway connections currently share a provider identity; refusing to mix them. Direct can be combined with either gateway.")
    if "omlx" in selection:
        effective = "both" if mode == "both" else "local"
    elif "model-gateway" in selection:
        effective = "cloud"
    elif "existing-gateway" in selection:
        effective = "existing-gateway"
    else:
        effective = "direct" if "direct" in selection else "later"
    return effective, [value for value in CHOICES if value in selection]


def validate(receipt):
    if "model_access" not in receipt:
        return  # pre-composition receipts are still supported
    access = receipt["model_access"]
    if (not isinstance(access, list) or any(not isinstance(v, str) or v not in CHOICES for v in access)
            or access != [v for v in CHOICES if v in access]):
        raise RuntimeError("Invalid saved model-access selection")
    if receipt.get("mode") not in MODES:
        raise RuntimeError("Invalid model-access topology")
    mode, normalized = resolve(receipt["mode"], access, None)
    if mode != receipt.get("mode") or normalized != access:
        raise RuntimeError("Inconsistent model-access topology")
    settings = receipt.get("settings", {})
    if (settings.get("PI_SHARED_DIRECT_ENABLED") != str(int("direct" in access))
            or settings.get("PI_SHARED_ENABLE_GATEWAY") != str(int(bool(set(access) & {"model-gateway", "existing-gateway"})))
            or ("model-gateway" in access) != ("model-gateway" in receipt["modules"])):
        raise RuntimeError("Inconsistent saved model-access policy")
