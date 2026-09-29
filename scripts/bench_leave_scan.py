"""Micro-benchmark: the O(|vars|) scan in add_leave_constraints at 500x28 scale."""
import datetime
import time

# Simulate vars dict: (nurse_id, role_id, date, shift_id) -> var
# 500 nurses x 28 days x 3 shifts x 1 role, current + prev period = 84k entries
nurses = [f"nurse-{i:04d}" for i in range(500)]
shifts = ["shift-a", "shift-b", "shift-c"]
role = "role-1"
dates = [datetime.date(2026, 9, 1) + datetime.timedelta(days=d) for d in range(28)]
ext_dates = [datetime.date(2026, 8, 4) + datetime.timedelta(days=d) for d in range(56)]

vars_dict = {}
for date in ext_dates:
    for nurse in nurses:
        for shift in shifts:
            vars_dict[(nurse, role, date, shift)] = 1  # stand-in var

print(f"vars dict size: {len(vars_dict)}")

# Simulate ~1000 (nurse, leave_date) pairs: 500 nurses x 2 leave days
leave_pairs = []
for i, nurse in enumerate(nurses):
    for d in range(2):
        leave_pairs.append((nurse, dates[(i + d * 13) % 28]))

# Current implementation pattern (constraints.py:157-164)
start = time.perf_counter()
hits = 0
for nurse_id, date in leave_pairs:
    keys = [k for k in vars_dict if k[0] == nurse_id and k[2] == date]
    hits += len(keys)
scan_time = time.perf_counter() - start
print(f"current linear-scan pattern: {scan_time:.2f}s ({hits} keys found)")

# Fixed pattern: direct key construction
nurse_roles = {n: [role] for n in nurses}
start = time.perf_counter()
hits2 = 0
for nurse_id, date in leave_pairs:
    for r in nurse_roles[nurse_id]:
        for s in shifts:
            if (nurse_id, r, date, s) in vars_dict:
                hits2 += 1
fixed_time = time.perf_counter() - start
print(f"direct-key pattern: {fixed_time:.4f}s ({hits2} keys found)")
print(f"speedup: {scan_time / max(fixed_time, 1e-9):.0f}x")
