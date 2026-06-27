import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

@Suite
final class WorkbenchRefreshCoordinatorTests {

    @Test @MainActor func refreshOnceCallsOperationAndReturnsRefreshed() async throws {
        var callCount = 0
        let coordinator = WorkbenchRefreshCoordinator(refreshInterval: .seconds(5)) {
            callCount += 1
        }

        let result = await coordinator.refreshOnce()

        #expect(result == .refreshed)
        #expect(callCount == 1)
    }

    @Test @MainActor func refreshOnceSkipsWhenAnotherRefreshIsInProgress() async throws {
        let stream = AsyncStream<Void>.makeStream()
        var startedCount = 0

        let coordinator = WorkbenchRefreshCoordinator(refreshInterval: .seconds(5)) {
            startedCount += 1
            // Suspend until the test resumes the stream.
            for await _ in stream.stream {
                break
            }
        }

        let firstTask = Task { @MainActor in
            await coordinator.refreshOnce()
        }

        // Wait until the first refresh has entered the operation.
        while startedCount == 0 {
            await Task.yield()
        }

        let secondResult = await coordinator.refreshOnce()
        #expect(secondResult == .skippedInProgress)

        // Allow the first refresh to finish and verify it reported success.
        stream.continuation.yield(())
        let firstResult = await firstTask.value
        #expect(firstResult == .refreshed)
        #expect(startedCount == 1)
    }

    @Test @MainActor func defaultRefreshIntervalIsFiveSeconds() async throws {
        let coordinator = WorkbenchRefreshCoordinator {
            // no-op
        }

        #expect(coordinator.refreshInterval == .seconds(5))
    }

    @Test @MainActor func startPeriodicRefreshPollsOnIntervalUntilCancelled() async throws {
        let stream = AsyncStream<Void>.makeStream()
        var callCount = 0

        let coordinator = WorkbenchRefreshCoordinator(refreshInterval: .milliseconds(100)) {
            callCount += 1
            // Block the first refresh so we can observe the loop starts.
            if callCount == 1 {
                for await _ in stream.stream {
                    break
                }
            }
        }

        let task = Task { @MainActor in
            await coordinator.startPeriodicRefresh()
        }

        // Wait until the loop has performed its first refresh.
        while callCount == 0 {
            await Task.yield()
        }

        // Release the first refresh and give the loop time to tick again.
        stream.continuation.yield(())
        try? await Task.sleep(for: .milliseconds(300))

        task.cancel()
        _ = await task.value

        #expect(callCount >= 2)
    }
}
