const actionErrors: Record<string, string> = {
  access_denied: '原账户访问或权限已变化，请核对原授权',
  scope_denied: '原账户访问或权限已变化，请核对原授权',
  authentication_required: '原账户访问或权限已变化，请核对原授权',
  connection_revoked: '原账户访问或权限已变化，请核对原授权',
  admission_unknown: '原受理或执行证据待恢复，请勿另发写入请求',
  job_terminal_unknown: '原受理或执行证据待恢复，请勿另发写入请求',
  interrupted_dispatch: '原受理或执行证据待恢复，请勿另发写入请求',
  background_execution_error: '原受理或执行证据待恢复，请勿另发写入请求',
  storage_error: '原受理或执行证据待恢复，请勿另发写入请求',
  transport_unknown: '结果未知，请按原动作编号对账',
  service_unavailable: '结果未知，请按原动作编号对账',
  not_found: '结果未知，请按原动作编号对账',
  readback_mismatch: '远端对象或版本与批准内容冲突',
  precondition_failed: '远端对象或版本与批准内容冲突',
  preflight_unsupported_target: '原日程包含本次未批准的参与者或扩展字段，未修改日程',
  preflight_etag_conflict: '原日程已更新，未修改日程，请读取新版本后提出草稿',
  preflight_read_failed: '原日程预检未完成，本次未进入修改，请核对原行动',
  artifact_access_revoked: '原始来源或授权已变化，请重新核对',
  memory_access_revoked: '原始来源或授权已变化，请重新核对',
  connection_source_revoked: '原始来源或授权已变化，请重新核对',
  approval_invalidated: '原始来源或授权已变化，请重新核对',
  execution_cancelled: '本次执行已停止，请核对原执行记录',
  execution_timeout: '本次执行超时，请核对原执行记录',
  execution_lease_error: '本次执行权已变化，请核对原执行记录',
}

export function actionErrorMessage(code?: string | null): string | null {
  return code ? Object.hasOwn(actionErrors, code) ? actionErrors[code] : '行动证据待核对' : null
}
