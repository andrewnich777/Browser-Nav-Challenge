// Extract challenge code from React fiber memoizedState
(() => {
    const root = document.getElementById("root");
    const containerKey = Object.keys(root).find(k => k.startsWith("__reactContainer"));
    if (!containerKey) return {error: "no container"};

    const codes = [];
    const visited = new WeakSet();

    function walkFiber(fiber, depth) {
        if (!fiber || depth > 50 || visited.has(fiber)) return;
        visited.add(fiber);

        // Check memoizedState chain
        let state = fiber.memoizedState;
        let stateIdx = 0;
        while (state && stateIdx < 30) {
            if (state.memoizedState && typeof state.memoizedState === "string") {
                if (/^[A-Z0-9]{6}$/.test(state.memoizedState)) {
                    codes.push({source: "memoizedState", value: state.memoizedState, depth: depth});
                }
            }
            // Check queue/baseState
            if (state.queue && state.queue.lastRenderedState) {
                const lrs = state.queue.lastRenderedState;
                if (typeof lrs === "string" && /^[A-Z0-9]{6}$/.test(lrs)) {
                    codes.push({source: "lastRenderedState", value: lrs, depth: depth});
                }
                if (typeof lrs === "object" && lrs !== null) {
                    for (const k of Object.keys(lrs)) {
                        const v = lrs[k];
                        if (typeof v === "string" && /^[A-Z0-9]{6}$/.test(v)) {
                            codes.push({source: "state." + k, value: v, depth: depth});
                        }
                    }
                }
            }
            state = state.next;
            stateIdx++;
        }

        // Check memoizedProps
        if (fiber.memoizedProps) {
            const props = fiber.memoizedProps;
            for (const k of Object.keys(props)) {
                const v = props[k];
                if (typeof v === "string" && /^[A-Z0-9]{6}$/.test(v)) {
                    codes.push({source: "props." + k, value: v, depth: depth});
                }
            }
        }

        // Walk children and siblings
        walkFiber(fiber.child, depth + 1);
        walkFiber(fiber.sibling, depth + 1);
    }

    walkFiber(root[containerKey].stateNode || root[containerKey], 0);
    return codes;
})()
