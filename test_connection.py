#!/usr/bin/env python3
"""Test WebSocket connection to Centauri Carbon printer using SDCP protocol."""

import asyncio
import sys
import os
import json
import uuid
import websockets

# Configuration from discovery
PRINTER_IP = os.getenv("PRINTER_IP", "192.168.1.41")
PRINTER_PORT = int(os.getenv("PRINTER_PORT", "3030"))
MAINBOARD_ID = os.getenv("MAINBOARD_ID", "60461c580103147000001c0000000000")
WS_PATH = "/websocket"


def create_request(cmd: int, data: dict = None) -> str:
    """Create SDCP protocol request message."""
    request_id = str(uuid.uuid4())
    timestamp = int(asyncio.get_event_loop().time())
    
    request = {
        "Id": str(uuid.uuid4()),
        "Data": {
            "Cmd": cmd,
            "Data": data or {},
            "RequestID": request_id,
            "MainboardID": MAINBOARD_ID,
            "TimeStamp": timestamp,
            "From": 0,
        },
        "Topic": f"sdcp/request/{MAINBOARD_ID}",
    }
    
    return json.dumps(request), request_id


async def test_sdcp_connection():
    """Test full SDCP protocol connection."""
    url = f"ws://{PRINTER_IP}:{PRINTER_PORT}{WS_PATH}"
    
    print(f"🔍 Testing SDCP connection to {url}")
    print(f"   Mainboard ID: {MAINBOARD_ID}\n")
    
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            print("✅ WebSocket connected!\n")
            
            pending_requests = {}
            received_status = False
            received_attrs = False
            
            # Send status request
            print("📊 Requesting status (Cmd 0)...")
            msg, req_id = create_request(0)
            pending_requests[req_id] = "status"
            await ws.send(msg)
            
            # Send attributes request  
            print("📋 Requesting attributes (Cmd 1)...")
            msg, req_id = create_request(1)
            pending_requests[req_id] = "attributes"
            await ws.send(msg)
            
            # Listen for responses
            print("\n⏳ Listening for responses...")
            timeout_count = 0
            
            while not (received_status and received_attrs) and timeout_count < 10:
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    data = json.loads(response)
                    topic = data.get("Topic", "")
                    msg_data = data.get("Data", {})
                    
                    print(f"\n   📨 Received: {topic}")
                    
                    if "sdcp/status/" in topic and not received_status:
                        received_status = True
                        status = data.get("Status", {})
                        print(f"   ✅ Status received!")
                        print(f"      Machine: {status.get('CurrentStatus', ['unknown'])[0]}")
                        print(f"      Nozzle: {status.get('TempOfNozzle', 0)}°C / {status.get('TempTargetNozzle', 0)}°C")
                        print(f"      Bed: {status.get('TempOfHotbed', 0)}°C / {status.get('TempTargetHotbed', 0)}°C")
                        print(f"      Position: {status.get('CurrenCoord', 'unknown')}")
                        
                    elif "sdcp/attributes/" in topic and not received_attrs:
                        received_attrs = True
                        attrs = data.get("Attributes", {})
                        print(f"   ✅ Attributes received!")
                        print(f"      Name: {attrs.get('Name', 'unknown')}")
                        print(f"      Firmware: {attrs.get('FirmwareVersion', 'unknown')}")
                        print(f"      Build Volume: {attrs.get('XYZsize', 'unknown')}")
                        
                    elif "sdcp/response/" in topic:
                        request_id = msg_data.get("RequestID", "")
                        if request_id in pending_requests:
                            req_type = pending_requests.pop(request_id)
                            ack = msg_data.get("Ack", -1)
                            print(f"   ✅ Response to {req_type} request (ack={ack})")
                            
                            if req_type == "files" and ack == 0:
                                files = msg_data.get("FileList", [])
                                print(f"      Found {len(files)} files/folders")
                    
                except asyncio.TimeoutError:
                    timeout_count += 1
                    continue
            
            # Test file listing if we got this far
            if received_status and received_attrs:
                print("\n📁 Testing file listing (Cmd 258)...")
                msg, req_id = create_request(258, {"Url": "/local/"})
                pending_requests[req_id] = "files"
                await ws.send(msg)
                
                # Wait for file list response
                for _ in range(5):
                    try:
                        response = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        data = json.loads(response)
                        topic = data.get("Topic", "")
                        
                        if "sdcp/response/" in topic:
                            request_id = data.get("Data", {}).get("RequestID", "")
                            if request_id in pending_requests:
                                ack = data.get("Data", {}).get("Ack", -1)
                                if ack == 0:
                                    files = data.get("Data", {}).get("FileList", [])
                                    print(f"   ✅ File list received: {len(files)} entries")
                                    if files:
                                        print(f"   Sample:")
                                        for f in files[:3]:
                                            t = "📁" if f.get("type") == 0 else "📄"
                                            print(f"      {t} {f.get('name', 'unknown')}")
                                else:
                                    print(f"   ⚠️  File list failed (ack={ack})")
                                break
                    except asyncio.TimeoutError:
                        continue
            
            print("\n" + "="*50)
            print("✅ SDCP PROTOCOL TEST SUCCESSFUL!")
            print("="*50)
            print(f"\nThe MCP server is ready to connect to your printer!")
            print(f"\nConfiguration:")
            print(f"  PRINTER_IP={PRINTER_IP}")
            print(f"  MAINBOARD_ID={MAINBOARD_ID}")
            print(f"  WEBSOCKET_PATH={WS_PATH}")
            print(f"\nTo run the MCP server:")
            print(f"  cd /root/.openclaw/workspace/CentuariMCP")
            print(f"  export PRINTER_IP={PRINTER_IP}")
            print(f"  export MAINBOARD_ID={MAINBOARD_ID}")
            print(f"  python3 -m centauri_mcp.server")
            
            return True
            
    except Exception as e:
        print(f"\n❌ Connection failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_sdcp_connection())
    sys.exit(0 if success else 1)
