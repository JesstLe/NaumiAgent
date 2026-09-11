export class ApiException extends Error {
  constructor(
    public code: ApiErrorCode,
    message: string,
    public status?: number,
    public cause?: unknown,
  ) {
    super(message)
    this.name = 'ApiException'
  }
}

export type ApiErrorCode =
  | 'InvalidUrl'
  | 'InvalidResponse'
  | 'AuthFailed'
  | 'SessionUnavailable'
  | 'MissingSelectedSession'
  | 'CapabilityUnavailable'
  | 'ProtocolVersionMismatch'
  | 'ServerError'
  | 'HttpStatus'
  | 'DecodingFailed'
  | 'NetworkFailure'

export function isApiException(error: unknown): error is ApiException {
  return error instanceof ApiException
}
