import requests
import json

url = "http://localhost:11434/v1/chat/completions"
headers = {"Content-Type": "application/json"}
data = {
    "model": "gemma-4-26B-A4B-it-heretic:latest",
    "messages": [{"role": "user", "content": "What is the weather in Paris? Call the get_weather function."}],
    "tools": [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a given location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"}
                },
                "required": ["location"]
            }
        }
    }],
    "stream": False
}

response = requests.post(url, headers=headers, data=json.dumps(data))
print(f"Status Code: {response.status_code}")
try:
    print(json.dumps(response.json(), indent=2))
except:
    print(response.text)
