import asyncio
import sys
import json
from typing import Optional
from contextlib import AsyncExitStack
import httpx
import os
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dotenv import load_dotenv

load_dotenv()  # load environment variables from .env

class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.api_key = "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"
        if not self.api_key:
            print("ERROR: OPENROUTER_API_KEY not found in environment variables")
            print("Please create a .env file with your API key")
            sys.exit(1)
            
        self.http_client = httpx.AsyncClient(
            base_url="https://openrouter.ai/api/v1",
            timeout=30.0,  # Add timeout to prevent hanging indefinitely
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "http://localhost:3000",
                "Content-Type": "application/json"
            }
        )

    async def connect_to_server(self, server_script_path: str):
        """Connect to an MCP server

        Args:
            server_script_path: Path to the server script (.py or .js)
        """
        print(f"Attempting to connect to server at {server_script_path}")
        
        # Verify the file exists
        if not os.path.exists(server_script_path):
            raise FileNotFoundError(f"Server script not found: {server_script_path}")
            
        is_python = server_script_path.endswith('.py')
        is_js = server_script_path.endswith('.js')
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = "python" if is_python else "node"
        server_params = StdioServerParameters(
            command=command,
            args=[server_script_path],
            env=None
        )

        try:
            print("Starting stdio_client connection...")
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            print("stdio_transport created")
            
            self.stdio, self.write = stdio_transport
            print("Creating client session...")
            self.session = await self.exit_stack.enter_async_context(ClientSession(self.stdio, self.write))
            print("Session created, initializing...")

            # Set a longer timeout for initialization
            init_task = asyncio.create_task(self.session.initialize())
            try:
                await asyncio.wait_for(init_task, timeout=30.0)  # Increased from 10.0 to 30.0
                print("Session initialized successfully")
            except asyncio.TimeoutError:
                print("ERROR: Session initialization timed out")
                raise TimeoutError("Failed to initialize session within timeout period")

            # List available tools
            print("Requesting tool list...")
            response = await self.session.list_tools()
            tools = response.tools
            print("\nConnected to server with tools:", [tool.name for tool in tools])
            
        except Exception as e:
            print(f"ERROR connecting to server: {str(e)}")
            print(traceback.format_exc())
            raise

    async def process_query(self, query: str) -> str:
        """Process a query using OpenRouter and available tools"""
        if not self.session:
            return "ERROR: Not connected to server"
            
        messages = [
            {
                "role": "user",
                "content": query
            }
        ]

        try:
            response = await self.session.list_tools()
            available_tools = [{
            "type": "function",
            "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.inputSchema
    }
} for tool in response.tools]


            print(f"Sending request to OpenRouter with {len(available_tools)} tools...")
            
            # Initial OpenRouter API call
            response = await self.http_client.post("/chat/completions", json={
               "model": "openai/gpt-3.5-turbo-1106",  # ✅ this one supports tools
               "messages": messages,
               "tools": available_tools,
               "max_tokens": 1024
              })

            
            if response.status_code != 200:
                print(f"ERROR: OpenRouter API returned status {response.status_code}")
                print(f"Response: {response.text}")
                return f"Error from OpenRouter API: {response.text}"
                
            response_data = response.json()
            print("Received response from OpenRouter:", json.dumps(response_data, indent=2)[:200] + "...")

            # Validate response structure
            if 'error' in response_data:
                return f"Error from OpenRouter: {response_data['error']}"

            if 'choices' not in response_data or not response_data['choices']:
                print("ERROR: Invalid response format from OpenRouter")
                print("Full response:", json.dumps(response_data, indent=2))
                return "Invalid response received from API"

            # Process response and handle tool calls
            final_text = []
            assistant_message = response_data['choices'][0].get('message', {})
            
            if assistant_message.get('content'):
                final_text.append(assistant_message['content'])
            
            if 'tool_calls' in assistant_message:
                for tool_call in assistant_message['tool_calls']:
                    tool_name = tool_call['function']['name']
                    tool_args = tool_call['function']['arguments']
                    tool_id = tool_call['id']

                    print(f"Calling tool: {tool_name}")
                    try:
                        parsed_args = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                        result = await self.session.call_tool(tool_name, parsed_args)
                        tool_result = result.content
                        print(f"Tool result received: {tool_result[:100]}...")
                    except Exception as e:
                        error_msg = f"Error executing tool {tool_name}: {str(e)}"
                        print(error_msg)
                        tool_result = error_msg
                    
                    final_text.append(f"[Tool {tool_name} result: {tool_result}]")

                    messages.extend([
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [tool_call]
                        },
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": tool_result
                        }
                    ])

                    # Get follow-up response
                    try:
                        response = await self.http_client.post("/chat/completions", json={
                            "model": "anthropic/claude-3-sonnet",
                            "messages": messages,
                            "tools": available_tools
                        })
                        
                        if response.status_code != 200:
                            raise Exception(f"API error: {response.text}")
                            
                        response_data = response.json()
                        if 'choices' in response_data and response_data['choices']:
                            next_message = response_data['choices'][0].get('message', {})
                            if next_message.get('content'):
                                final_text.append(next_message['content'])
                    except Exception as e:
                        error_msg = f"Error in follow-up request: {str(e)}"
                        print(error_msg)
                        final_text.append(error_msg)

            return "\n".join(filter(None, final_text))
            
        except Exception as e:
            error_msg = f"Error processing query: {str(e)}"
            print(error_msg)
            print(traceback.format_exc())
            return error_msg

    async def chat_loop(self):
        """Run an interactive chat loop"""
        print("\nMCP Client Started!")
        print("Type your queries or 'quit' to exit.")

        while True:
            try:
                query = input("\nQuery: ").strip()

                if query.lower() in ['quit', 'exit', 'q']:
                    break

                response = await self.process_query(query)
                print("\n" + response)

            except Exception as e:
                print(f"\nError: {str(e)}")
                print(traceback.format_exc())

    async def cleanup(self):
        """Clean up resources"""
        print("Cleaning up resources...")
        try:
            await self.exit_stack.aclose()
            await self.http_client.aclose()
            print("Cleanup complete")
        except Exception as e:
            print(f"Error during cleanup: {str(e)}")

async def main():
    if len(sys.argv) < 2:
        print("MCP Client - Connect to an MCP-compatible server")
        print("\nUsage:")
        print("  python client.py <path_to_server_script>")
        print("\nExample:")
        print("  python client.py ./server/example.py")
        sys.exit(1)

    client = MCPClient()
    try:
        # Use the command-line argument instead of hardcoded path
        await client.connect_to_server(sys.argv[1])
        await client.chat_loop()
    finally:
        await client.cleanup()

if __name__ == "__main__":
    asyncio.run(main())