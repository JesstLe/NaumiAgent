import { AppRoutes } from '@/routes'
import { useWorkspace } from '@/hooks/WorkspaceProvider'

export function ConnectionBootstrap() {
  const workspace = useWorkspace()
  return (
    <>
      {!workspace.daemon && (
        <div
          role="status"
          className="bg-amber-50 px-4 py-2 text-sm text-amber-900"
        >
          {workspace.connecting ? '正在连接本地服务…' : workspace.error}
          {!workspace.connecting && (
            <button
              className="ml-3 underline"
              onClick={() => void workspace.connect()}
            >
              重新连接
            </button>
          )}
        </div>
      )}
      <AppRoutes />
    </>
  )
}
