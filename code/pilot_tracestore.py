"""Pilot for K8: storage of per-call LLM logs reconstructed from agent trajectories.

Observability stores (LangSmith/Langfuse/OTel GenAI spans) record the full input
message list of every LLM call. For an append-only ReAct trajectory, the input of
the call that produced assistant message i is messages[:i]. We reconstruct those
per-call records and compare storage layouts on bytes and random-call latency.
"""
import hashlib
import json
import os
import random
import sys
import time
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard as zstd


def calls_from_messages(msgs):
    """Yield (call_idx, input_messages) for every assistant turn."""
    k = 0
    for i, m in enumerate(msgs):
        if m.get("role") in ("assistant", "ai"):
            yield k, msgs[:i]
            k += 1


def canon(m):
    return json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_dab(zip_path, n_runs, seed):
    z = zipfile.ZipFile(zip_path)
    names = sorted(n for n in z.namelist() if n.endswith(".json"))
    random.Random(seed).shuffle(names)
    for name in names[:n_runs]:
        yield name, json.loads(z.read(name))


def load_swe(parquet_path, n_runs, seed):
    t = pq.read_table(parquet_path)
    cols = t.column_names
    col = "trajectory" if "trajectory" in cols else ("messages" if "messages" in cols else cols[-1])
    idx = list(range(t.num_rows))
    random.Random(seed).shuffle(idx)
    for i in idx[:n_runs]:
        v = t.column(col)[i].as_py()
        msgs = json.loads(v) if isinstance(v, str) else v
        msgs = [{"role": (m.get("role") or "user"), "content": m.get("content") or m.get("text") or ""}
                for m in msgs if isinstance(m, dict)]
        yield f"swe/{i}", msgs


def build(runs, workdir):
    os.makedirs(workdir, exist_ok=True)
    records = []           # (session, call_idx, json_bytes)
    msg_store = {}         # hash -> canonical message bytes
    dag = []               # (session, call_idx, parent_ref, new_msg_hashes)
    for sess, msgs in runs:
        prev_len = 0
        for k, inp in calls_from_messages(msgs):
            rec = json.dumps({"session": sess, "call": k, "input": inp}, ensure_ascii=False).encode()
            records.append((sess, k, rec))
            new = inp[prev_len:]
            hs = []
            for m in new:
                b = canon(m).encode()
                h = hashlib.blake2b(b, digest_size=16).hexdigest()
                msg_store.setdefault(h, b)
                hs.append(h)
            dag.append((sess, k, (sess, k - 1) if k > 0 else None, hs))
            prev_len = len(inp)
    return records, msg_store, dag


def bench(records, msg_store, dag, workdir, n_probe=500, seed=0):
    out = {}
    raw = sum(len(r[2]) for r in records)
    out["n_calls"] = len(records)
    out["raw_bytes"] = raw
    cctx = zstd.ZstdCompressor(level=3)
    dctx = zstd.ZstdDecompressor()
    probes = random.Random(seed).sample(range(len(records)), min(n_probe, len(records)))

    # V1: per-record zstd (row-level compression, random access by offset)
    blobs = [cctx.compress(r[2]) for r in records]
    out["v1_rowzstd_bytes"] = sum(len(b) for b in blobs)
    t = time.perf_counter()
    for p in probes:
        json.loads(dctx.decompress(blobs[p]))
    out["v1_rowzstd_ms_per_call"] = 1e3 * (time.perf_counter() - t) / len(probes)

    # V2: Parquet + zstd (columnar, page/row-group compression)
    path = os.path.join(workdir, "v2.parquet")
    tbl = pa.table({"session": [r[0] for r in records], "call": [r[1] for r in records],
                    "payload": [r[2] for r in records]})
    pq.write_table(tbl, path, compression="zstd", row_group_size=1024)
    out["v2_parquet_bytes"] = os.path.getsize(path)
    pf = pq.ParquetFile(path)
    t = time.perf_counter()
    for p in probes[:100]:
        rg = p // 1024
        col = pf.read_row_group(rg, columns=["payload"]).column(0)
        json.loads(col[p % 1024].as_py())
    out["v2_parquet_ms_per_call"] = 1e3 * (time.perf_counter() - t) / min(100, len(probes))

    # V3: whole-stream zstd with long-distance matching (best generic ratio, no random access)
    params = zstd.ZstdCompressionParameters.from_level(19, window_log=27, enable_ldm=True)
    big = b"\n".join(r[2] for r in records)
    comp = zstd.ZstdCompressor(compression_params=params).compress(big)
    out["v3_streamzstd_long_bytes"] = len(comp)
    t = time.perf_counter()
    zstd.ZstdDecompressor().decompress(comp, max_output_size=len(big) + 1)
    out["v3_full_decompress_s"] = time.perf_counter() - t

    # V4: message-level content addressing + prefix DAG (each call = parent + new msg ids)
    msg_blobs = {h: cctx.compress(b) for h, b in msg_store.items()}
    dag_bytes = sum(16 * len(hs) + 24 for _, _, _, hs in dag)
    out["v4_dag_bytes"] = sum(len(b) for b in msg_blobs.values()) + dag_bytes
    index = {(s, k): (parent, hs) for s, k, parent, hs in dag}
    t = time.perf_counter()
    for p in probes:
        s, k, _ = records[p]
        chain, node = [], (s, k)
        while node is not None:
            parent, hs = index[node]
            chain.append(hs)
            node = parent
        msgs = [json.loads(dctx.decompress(msg_blobs[h])) for hs in reversed(chain) for h in hs]
    out["v4_dag_ms_per_call"] = 1e3 * (time.perf_counter() - t) / len(probes)
    out["unique_messages"] = len(msg_store)
    out["unique_message_bytes"] = sum(len(b) for b in msg_store.values())
    return out


def main():
    src, path, n_runs, workdir = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    runs = list(load_dab(path, n_runs, 0) if src == "dab" else load_swe(path, n_runs, 0))
    records, msg_store, dag = build(runs, workdir)
    res = bench(records, msg_store, dag, workdir)
    res.update({"source": src, "n_sessions": len(runs)})
    for k in ["raw_bytes", "v1_rowzstd_bytes", "v2_parquet_bytes", "v3_streamzstd_long_bytes", "v4_dag_bytes"]:
        res[k + "_ratio"] = round(res["raw_bytes"] / res[k], 2)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
