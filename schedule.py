"""Choose workers against the official integer-millisecond objective."""

def schedule(numbers, durations):
    if not numbers:
        return []
    jobs = sorted(numbers, key=lambda n: (-durations[n], n))
    total = sum(durations[n] for n in jobs)
    longest = durations[jobs[0]]
    best_cost = total + 2000
    best = [jobs]
    for count in range(2, min(8, len(jobs)) + 1):
        floor = max(longest, (total + count - 1) // count)
        if floor + 2000 * count >= best_cost:
            continue
        loads = [0] * count
        groups = [[] for _ in range(count)]
        for n in jobs:
            k = loads.index(min(loads))
            groups[k].append(n)
            loads[k] += durations[n]
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best_cost, best = cost, groups
    return best
