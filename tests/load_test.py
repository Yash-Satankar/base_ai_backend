# tests/load_test.py
import asyncio
import time
import httpx

BASE_URL = "http://localhost:8000"

async def send_health_request(client: httpx.AsyncClient, req_id: int) -> float:
    start = time.time()
    try:
        response = await client.get(f"{BASE_URL}/health/full")
        duration = time.time() - start
        if response.status_code == 200:
            return duration
        else:
            print(f"Request {req_id} failed with status: {response.status_code}")
            return -1.0
    except Exception as e:
        print(f"Request {req_id} encountered exception: {e}")
        return -1.0

async def send_match_rules_request(client: httpx.AsyncClient, req_id: int) -> float:
    start = time.time()
    payload = {
        "requirement": "We need a corporate enterprise employee management system with payroll, time tracking, and manager approvals."
    }
    try:
        response = await client.post(f"{BASE_URL}/planner/match-rules", json=payload)
        duration = time.time() - start
        if response.status_code == 200:
            return duration
        else:
            print(f"Match Rules {req_id} failed with status: {response.status_code}")
            return -1.0
    except Exception as e:
        print(f"Match Rules {req_id} encountered exception: {e}")
        return -1.0

async def run_load_test():
    print("🚀 Starting BaseAI Production Load Test...")
    print(f"Target URL: {BASE_URL}")
    
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=200)
    async with httpx.AsyncClient(limits=limits, timeout=10.0) as client:
        # Phase 1: 100 Concurrent Health Checks
        print("\n--- Phase 1: 100 Concurrent Health Checks ---")
        start_time = time.time()
        tasks = [send_health_request(client, i) for i in range(100)]
        results = await asyncio.gather(*tasks)
        total_duration = time.time() - start_time
        
        successful_runs = [r for r in results if r > 0]
        failures = len(results) - len(successful_runs)
        
        if successful_runs:
            avg_latency = sum(successful_runs) / len(successful_runs)
            max_latency = max(successful_runs)
            min_latency = min(successful_runs)
        else:
            avg_latency, max_latency, min_latency = 0, 0, 0
            
        print(f"Finished 100 requests in {total_duration:.2f}s")
        print(f"Success Rate   : {len(successful_runs)}/100 ({len(successful_runs)}%)")
        print(f"Failures       : {failures}")
        print(f"Average Latency: {avg_latency*1000:.2f}ms")
        print(f"Min Latency    : {min_latency*1000:.2f}ms")
        print(f"Max Latency    : {max_latency*1000:.2f}ms")

        # Phase 2: 20 Concurrent Rule Matches (Simulating generation trigger load)
        print("\n--- Phase 2: 20 Concurrent Rule Match Operations ---")
        start_time = time.time()
        tasks = [send_match_rules_request(client, i) for i in range(20)]
        results = await asyncio.gather(*tasks)
        total_duration = time.time() - start_time
        
        successful_runs = [r for r in results if r > 0]
        failures = len(results) - len(successful_runs)
        
        if successful_runs:
            avg_latency = sum(successful_runs) / len(successful_runs)
            max_latency = max(successful_runs)
            min_latency = min(successful_runs)
        else:
            avg_latency, max_latency, min_latency = 0, 0, 0
            
        print(f"Finished 20 requests in {total_duration:.2f}s")
        print(f"Success Rate   : {len(successful_runs)}/20 ({len(successful_runs)}%)")
        print(f"Failures       : {failures}")
        print(f"Average Latency: {avg_latency*1000:.2f}ms")
        print(f"Min Latency    : {min_latency*1000:.2f}ms")
        print(f"Max Latency    : {max_latency*1000:.2f}ms")

if __name__ == "__main__":
    asyncio.run(run_load_test())
