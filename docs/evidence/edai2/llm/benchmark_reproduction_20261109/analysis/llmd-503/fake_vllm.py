import json, os, threading, time
BUSY_DELAY=float(os.environ.get("METRICS_DELAY_BUSY","0"))
MAX_SEQS=int(os.environ.get("MAX_SEQS","1000")); SVC=float(os.environ.get("SVC_SECONDS","1"))
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
running = 0; lock = threading.Lock(); total = 0
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body.encode(); self.send_response(code); self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/metrics"):
            with lock: r = running; t = total
            if r and BUSY_DELAY: time.sleep(BUSY_DELAY)
            m = ('# TYPE vllm:num_requests_running gauge\n# TYPE vllm:num_requests_waiting gauge\n# TYPE vllm:kv_cache_usage_perc gauge\n# TYPE vllm:gpu_cache_usage_perc gauge\n# TYPE vllm:cache_config_info gauge\n' + f'vllm:num_requests_running{{model_name="m"}} {min(r,MAX_SEQS)}\nvllm:num_requests_waiting{{model_name="m"}} {max(int(os.environ.get("FORCE_WAITING","0")),r-MAX_SEQS)}\n'
                 f'vllm:kv_cache_usage_perc{{model_name="m"}} {0.01*r}\nvllm:gpu_cache_usage_perc{{model_name="m"}} {0.01*r}\n'
                 f'vllm:cache_config_info{{block_size="16",num_gpu_blocks="1000"}} 1\nfake_requests_total {t}\n')
            return self._send(200, m, "text/plain")
        if self.path.startswith("/v1/models"):
            return self._send(200, json.dumps({"data": [{"id": "m"}]}))
        self._send(200, "ok", "text/plain")
    def do_POST(self):
        global running, total
        n = int(self.headers.get("content-length", 0)); self.rfile.read(n)
        CAP=int(os.environ.get('QUEUE_CAP','0'))
        with lock:
            if CAP and running + 1 > CAP:
                rej=True
            else:
                rej=False; running += 1; total += 1
        if rej:
            print('REJECT 503', flush=True); return self._send(503, json.dumps({'error': {'message': 'queue full', 'code': 503}}))
        print("POST", self.path, flush=True)
        time.sleep(SVC)
        with lock: running -= 1
        self._send(200, json.dumps({"id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "a"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}))
ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
