"""Evaluation harness for V3 solver. Runs N episodes, computes metrics, detects regressions."""

import argparse
import json
import sys
import io
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)


@dataclass
class EpisodeResult:
    version: int = 0
    steps_solved: int = 0
    steps_attempted: int = 0
    time_total_sec: float = 0.0
    time_per_step: list = field(default_factory=list)
    failure_reasons: list = field(default_factory=list)
    vision_calls: int = 0
    recipe_hits: int = 0
    recipe_misses: int = 0
    passive_hits: int = 0
    system2_successes: int = 0
    recipes_promoted: int = 0
    estimated_cost_usd: float = 0.0


@dataclass
class FailureCluster:
    category: str = ""
    count: int = 0
    steps: list = field(default_factory=list)
    sample_reason: str = ""


@dataclass
class BenchmarkResult:
    episodes: list = field(default_factory=list)
    n_episodes: int = 0
    avg_solved: float = 0.0
    avg_time: float = 0.0
    avg_recipe_hit_rate: float = 0.0
    avg_vision_call_rate: float = 0.0
    failure_clusters: list = field(default_factory=list)
    improvement_suggestions: list = field(default_factory=list)


def run_episode(headed: bool = False, max_steps: int = 30) -> EpisodeResult:
    """Single episode: start browser, solve steps, return results."""
    from playwright.sync_api import sync_playwright
    from orchestrator import Orchestrator
    from verify import is_finish_page, extract_step_from_url, extract_version_from_url

    result = EpisodeResult(steps_attempted=max_steps)
    orch = Orchestrator()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page, version = orch.start_fresh(browser)
        result.version = version

        step = 1
        episode_start = time.time()

        while step <= min(max_steps, 30):
            step_start = time.time()

            url_step = extract_step_from_url(page.url)
            url_ver = extract_version_from_url(page.url)
            if url_ver:
                version = url_ver

            if url_step is None:
                if is_finish_page(page):
                    for s in range(step, 31):
                        result.steps_solved += 1
                    break
                break

            if url_step != step:
                if url_step > step:
                    for skipped in range(step, url_step):
                        result.steps_solved += 1
                    step = url_step
                    continue
                else:
                    break

            advanced, agents_used = orch.run_step(page, step, version)
            step_time = time.time() - step_start
            result.time_per_step.append(step_time)

            if advanced:
                result.steps_solved += 1
                if step == 30 or step == max_steps:
                    break
                step += 1
                page.wait_for_timeout(300)
            else:
                result.failure_reasons.append({
                    'step': step,
                    'reason': 'no_code_found',
                    'agents_tried': agents_used,
                })
                step += 1
                continue

        result.time_total_sec = time.time() - episode_start
        result.steps_attempted = min(max_steps, 30)

        # Pull metrics from orchestrator
        metrics = orch.get_full_metrics()
        result.vision_calls = metrics.get('vision_calls', 0)
        result.recipe_hits = metrics.get('recipe_hits', 0)
        result.recipe_misses = metrics.get('recipe_misses', 0)
        result.passive_hits = metrics.get('passive_hits', 0)
        result.system2_successes = metrics.get('system2_successes', 0)
        result.recipes_promoted = metrics.get('recipes_promoted', 0)

        # Estimate cost: ~$0.02 per vision call (Sonnet 4.5 with screenshot)
        result.estimated_cost_usd = result.vision_calls * 0.02

        browser.close()

    return result


def run_benchmark(n_episodes: int = 3, max_steps: int = 30,
                  headed: bool = False) -> BenchmarkResult:
    """Run N episodes, aggregate metrics."""
    result = BenchmarkResult(n_episodes=n_episodes)

    for i in range(n_episodes):
        print(f"\n{'='*60}", flush=True)
        print(f"EPISODE {i+1}/{n_episodes}", flush=True)
        print(f"{'='*60}", flush=True)

        episode = run_episode(headed=headed, max_steps=max_steps)
        result.episodes.append(episode)

        print(f"\nEpisode {i+1}: {episode.steps_solved}/{episode.steps_attempted} solved "
              f"({episode.time_total_sec:.1f}s, "
              f"recipe_hits={episode.recipe_hits}, "
              f"vision_calls={episode.vision_calls}, "
              f"promoted={episode.recipes_promoted})", flush=True)

    # Aggregate
    if result.episodes:
        result.avg_solved = sum(e.steps_solved for e in result.episodes) / len(result.episodes)
        result.avg_time = sum(e.time_total_sec for e in result.episodes) / len(result.episodes)

        total_steps = sum(e.steps_attempted for e in result.episodes)
        total_recipe_hits = sum(e.recipe_hits for e in result.episodes)
        total_vision = sum(e.vision_calls for e in result.episodes)

        result.avg_recipe_hit_rate = total_recipe_hits / max(total_steps, 1)
        result.avg_vision_call_rate = total_vision / max(total_steps, 1)

    # Failure analysis
    result.failure_clusters = analyze_failures(result)
    result.improvement_suggestions = suggest_improvements(result.failure_clusters)

    return result


def compare_runs(baseline: BenchmarkResult, current: BenchmarkResult) -> str:
    """Compare two benchmark results, report deltas and regressions."""
    lines = ["Benchmark Comparison:"]

    def delta(name, old, new, higher_better=True):
        diff = new - old
        arrow = "+" if diff > 0 else ""
        good = (diff > 0) == higher_better
        indicator = "OK" if good else "REGRESSION"
        lines.append(f"  {name}: {old:.2f} -> {new:.2f} ({arrow}{diff:.2f}) [{indicator}]")

    delta("avg_solved", baseline.avg_solved, current.avg_solved, higher_better=True)
    delta("avg_time", baseline.avg_time, current.avg_time, higher_better=False)
    delta("recipe_hit_rate", baseline.avg_recipe_hit_rate, current.avg_recipe_hit_rate,
          higher_better=True)
    delta("vision_call_rate", baseline.avg_vision_call_rate, current.avg_vision_call_rate,
          higher_better=False)

    return "\n".join(lines)


def analyze_failures(result: BenchmarkResult) -> list[FailureCluster]:
    """Cluster failures by type + error signature."""
    clusters = {}
    for episode in result.episodes:
        for f in episode.failure_reasons:
            reason = f.get('reason', 'unknown')
            if reason not in clusters:
                clusters[reason] = FailureCluster(
                    category=reason,
                    sample_reason=reason,
                )
            clusters[reason].count += 1
            clusters[reason].steps.append(f.get('step', 0))

    return sorted(clusters.values(), key=lambda c: c.count, reverse=True)


def suggest_improvements(clusters: list[FailureCluster]) -> list[str]:
    """Generate improvement suggestions based on failure clusters."""
    suggestions = []
    for c in clusters[:5]:
        if c.category == 'no_code_found':
            suggestions.append(
                f"{c.count} failures from 'no_code_found' at steps "
                f"{c.steps[:5]} -- System 2 couldn't extract code. "
                f"Consider: longer timeouts, more action rounds, or "
                f"specialized extraction for these challenge types."
            )
        elif c.category == 'stale_code':
            suggestions.append(
                f"{c.count} failures from 'stale_code' -- "
                f"passive check threshold may be too low."
            )
        elif c.category == 'wrong_frame':
            suggestions.append(
                f"{c.count} failures from 'wrong_frame' -- "
                f"iframe detection needs enhancement."
            )
        else:
            suggestions.append(
                f"{c.count} failures of type '{c.category}' at steps {c.steps[:5]}"
            )
    return suggestions


def print_report(result: BenchmarkResult):
    """Print formatted benchmark report."""
    print(f"\n{'='*60}", flush=True)
    print(f"BENCHMARK REPORT ({result.n_episodes} episodes)", flush=True)
    print(f"{'='*60}", flush=True)

    print(f"\nAvg steps solved:    {result.avg_solved:.1f}/30", flush=True)
    print(f"Avg time:            {result.avg_time:.1f}s", flush=True)
    print(f"Recipe hit rate:     {result.avg_recipe_hit_rate:.1%}", flush=True)
    print(f"Vision call rate:    {result.avg_vision_call_rate:.1%}", flush=True)

    total_cost = sum(e.estimated_cost_usd for e in result.episodes)
    print(f"Total cost:          ${total_cost:.2f}", flush=True)

    # Per-episode breakdown
    print(f"\nPer-episode:", flush=True)
    for i, ep in enumerate(result.episodes):
        print(f"  Episode {i+1}: {ep.steps_solved}/{ep.steps_attempted} "
              f"({ep.time_total_sec:.1f}s) "
              f"[recipe={ep.recipe_hits}, passive={ep.passive_hits}, "
              f"system2={ep.system2_successes}, promoted={ep.recipes_promoted}]",
              flush=True)

    # Compounding check
    if len(result.episodes) >= 2:
        e1 = result.episodes[0]
        e_last = result.episodes[-1]
        print(f"\nCompounding check (episode 1 vs {len(result.episodes)}):", flush=True)
        print(f"  Recipe hits:  {e1.recipe_hits} -> {e_last.recipe_hits} "
              f"({'UP' if e_last.recipe_hits > e1.recipe_hits else 'FLAT/DOWN'})",
              flush=True)
        print(f"  Vision calls: {e1.vision_calls} -> {e_last.vision_calls} "
              f"({'DOWN' if e_last.vision_calls < e1.vision_calls else 'FLAT/UP'})",
              flush=True)

    # Failure clusters
    if result.failure_clusters:
        print(f"\nFailure clusters:", flush=True)
        for c in result.failure_clusters[:5]:
            print(f"  {c.category}: {c.count}x (steps: {c.steps[:5]})", flush=True)

    # Suggestions
    if result.improvement_suggestions:
        print(f"\nImprovement suggestions:", flush=True)
        for s in result.improvement_suggestions:
            print(f"  - {s}", flush=True)


def save_history(result: BenchmarkResult):
    """Append episode results to benchmark_history.jsonl for trend analysis."""
    history_path = Path("knowledge/benchmark_history.jsonl")
    history_path.parent.mkdir(exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as f:
        for ep in result.episodes:
            f.write(json.dumps(asdict(ep), default=str) + "\n")


def main():
    parser = argparse.ArgumentParser(description="V3 Benchmark Harness")
    parser.add_argument("--episodes", type=int, default=3, help="Number of episodes")
    parser.add_argument("--max-steps", type=int, default=30, help="Max steps per episode")
    parser.add_argument("--headed", action="store_true", help="Show browser")
    args = parser.parse_args()

    result = run_benchmark(
        n_episodes=args.episodes,
        max_steps=args.max_steps,
        headed=args.headed,
    )

    print_report(result)
    save_history(result)
    print(f"\nHistory saved to knowledge/benchmark_history.jsonl", flush=True)


if __name__ == "__main__":
    main()
