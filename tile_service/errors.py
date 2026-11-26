"""Typed errors mapped to HTTP status codes by the server."""


class TileServiceError(Exception):
    status = 400
    code = "tile_service_error"

    def __init__(self, message: str | None = None, *, status: int | None = None, **details):
        super().__init__(message or self.__class__.__doc__ or self.code)
        if status is not None:
            self.status = int(status)
        self.details = details

    def to_dict(self) -> dict:
        data = {"error": self.code, "message": str(self)}
        if self.details:
            data["details"] = self.details
        return data


class ValidationError(TileServiceError):
    status = 400
    code = "invalid_request"


class ConflictError(TileServiceError):
    status = 409
    code = "version_conflict"


class ClaimDeniedError(TileServiceError):
    status = 403
    code = "claim_denied"


class NotFoundError(TileServiceError):
    status = 404
    code = "not_found"


class TileUnavailableError(TileServiceError):
    status = 503
    code = "tile_unavailable"


class CapacityError(TileServiceError):
    status = 507
    code = "insufficient_storage"


class ReferencedObjectError(TileServiceError):
    status = 409
    code = "object_still_referenced"


class PickupStateError(TileServiceError):
    status = 409
    code = "pickup_state_conflict"
