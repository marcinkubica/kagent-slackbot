#!/usr/bin/env python3
"""
Slack Bot Implementation with Socket Mode (WebSocket)

This implementation includes:
1. WebSocket-based real-time communication via Socket Mode
2. Rate limiting and security features
3. Input validation and sanitization
4. Security logging and monitoring
5. A2A protocol integration with kagent
6. Comprehensive error handling
7. App-level token support for Socket Mode
"""

import asyncio
import json
import logging
import time
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import os
import re
import ssl
from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientTimeout, WSMsgType
import websockets
import prometheus_client
from prometheus_client import Counter, Histogram, Gauge
import structlog

# Security imports
import validators

# Local imports
from config_validator import load_and_validate_config, SlackConfig, ConfigValidator, ConfigError

# Metrics for monitoring
WEBSOCKET_CONNECTIONS = Gauge('slack_bot_websocket_connections', 'Active WebSocket connections')
WEBSOCKET_MESSAGES = Counter('slack_bot_websocket_messages_total', 'Total WebSocket messages', ['type', 'status'])
WEBSOCKET_DURATION = Histogram('slack_bot_websocket_message_duration_seconds', 'Message processing duration')
RATE_LIMIT_EXCEEDED = Counter('slack_bot_rate_limit_exceeded_total', 'Rate limit exceeded')
CONNECTION_ERRORS = Counter('slack_bot_connection_errors_total', 'WebSocket connection errors')
AGENT_INVOCATIONS = Counter('slack_bot_agent_invocations_total', 'Agent invocations', ['agent', 'status'])

# Configure structured logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = structlog.get_logger()

class SecurityConfig:
    """Security configuration and constants"""
    
    # Rate limiting
    RATE_LIMIT_WINDOW = int(os.getenv('RATE_LIMIT_WINDOW', '60'))  # seconds
    RATE_LIMIT_MAX_REQUESTS = int(os.getenv('RATE_LIMIT_MAX_REQUESTS', '100'))
    
    # WebSocket timeouts
    WEBSOCKET_TIMEOUT = int(os.getenv('WEBSOCKET_TIMEOUT', '30'))
    PING_INTERVAL = int(os.getenv('PING_INTERVAL', '30'))
    PING_TIMEOUT = int(os.getenv('PING_TIMEOUT', '10'))
    
    # Connection retry
    MAX_RECONNECT_ATTEMPTS = int(os.getenv('MAX_RECONNECT_ATTEMPTS', '5'))
    RECONNECT_DELAY = int(os.getenv('RECONNECT_DELAY', '5'))

class RateLimiter:
    """Thread-safe rate limiter with user-based tracking"""
    
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = {}
        self.lock = asyncio.Lock()
    
    async def is_allowed(self, user_id: str) -> bool:
        """Check if request is allowed based on rate limits"""
        async with self.lock:
            now = time.time()
            window_start = now - self.window_seconds
            
            # Clean old entries
            if user_id in self.requests:
                self.requests[user_id] = [
                    req_time for req_time in self.requests[user_id] 
                    if req_time > window_start
                ]
            else:
                self.requests[user_id] = []
            
            # Check rate limit
            if len(self.requests[user_id]) >= self.max_requests:
                RATE_LIMIT_EXCEEDED.inc()
                return False
            
            # Add current request
            self.requests[user_id].append(now)
            return True

class InputValidator:
    """Input validation and sanitization"""
    
    @staticmethod
    def validate_socket_mode_payload(data: Dict[str, Any]) -> tuple[bool, str]:
        """Validate Socket Mode payload structure"""
        try:
            # Check required fields for Socket Mode
            if 'type' not in data:
                return False, "Missing 'type' field"
            
            # Validate Socket Mode message types
            valid_types = ['hello', 'events_api', 'interactive', 'slash_commands', 'disconnect']
            if data['type'] not in valid_types:
                return False, f"Invalid message type: {data['type']}"
            
            # For events_api messages, validate the nested payload
            if data['type'] == 'events_api':
                if 'payload' not in data:
                    return False, "Missing 'payload' field in events_api message"
                
                payload = data['payload']
                
                # Validate team_id format
                if 'team_id' in payload:
                    if not re.match(r'^T[A-Z0-9]{8,}$', payload['team_id']):
                        return False, "Invalid team_id format"
                
                # Validate user_id format if present in event
                if 'event' in payload and 'user' in payload['event']:
                    user_id = payload['event']['user']
                    if not re.match(r'^U[A-Z0-9]{8,}$', user_id):
                        return False, "Invalid user_id format"
                
                # Validate channel_id format if present in event
                if 'event' in payload and 'channel' in payload['event']:
                    channel_id = payload['event']['channel']
                    if not re.match(r'^[CDG][A-Z0-9]{8,}$', channel_id):
                        return False, "Invalid channel_id format"
            
            return True, "Valid"
            
        except Exception as e:
            return False, f"Validation error: {str(e)}"
    
    @staticmethod
    def sanitize_text(text: str, max_length: int = 3000) -> str:
        """Sanitize text input"""
        if not isinstance(text, str):
            return ""
        
        # Truncate to max length
        text = text[:max_length]
        
        # Remove potentially dangerous characters
        text = re.sub(r'[<>"\']', '', text)
        
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text

class A2AClient:
    """Secure A2A protocol client for kagent integration"""
    
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.timeout = ClientTimeout(total=timeout)
    
    async def invoke_agent(self, agent_name: str, task: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Invoke kagent agent via A2A protocol"""
        try:
            # Sanitize inputs
            agent_name = InputValidator.sanitize_text(agent_name, 100)
            task = InputValidator.sanitize_text(task, 2000)
            
            # Validate agent name format
            if not re.match(r'^[a-zA-Z0-9-_]+$', agent_name):
                raise ValueError("Invalid agent name format")
            
            url = f"{self.base_url}/kagent/{agent_name}"
            
            payload = {
                "task": task,
                "input_mode": "text",
                "output_mode": "text"
            }
            
            if session_id:
                payload["session_id"] = session_id
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload, headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'SlackBot-SocketMode/1.0'
                }) as response:
                    if response.status == 200:
                        result = await response.json()
                        AGENT_INVOCATIONS.labels(agent=agent_name, status='success').inc()
                        return result
                    else:
                        error_text = await response.text()
                        AGENT_INVOCATIONS.labels(agent=agent_name, status='error').inc()
                        raise Exception(f"A2A request failed: {response.status} - {error_text}")
                        
        except Exception as e:
            logger.error("A2A invocation failed", 
                        agent=agent_name, 
                        error=str(e))
            AGENT_INVOCATIONS.labels(agent=agent_name, status='error').inc()
            raise

class SlackSocketModeBot:
    """Slack bot using Socket Mode with WebSocket connections"""
    
    def __init__(self):
        # Load and validate configuration
        self.config = load_and_validate_config(strict=False)  # Allow warnings in production
        
        # Extract configuration for easy access
        self.app_token = self.config.app_token
        self.bot_token = self.config.bot_token
        self.team_id = self.config.team_id
        self.channel_ids = set(self.config.channel_ids)
        self.bot_keywords = self.config.bot_keywords
        
        logger.info("Bot configuration loaded", 
                   team_id=self.team_id,
                   channels=len(self.channel_ids),
                   keywords=self.bot_keywords)
        
        # Initialize security components
        self.rate_limiter = RateLimiter(
            SecurityConfig.RATE_LIMIT_MAX_REQUESTS,
            SecurityConfig.RATE_LIMIT_WINDOW
        )
        self.validator = InputValidator()
        
        # Initialize A2A client
        self.a2a_client = A2AClient(self.config.kagent_a2a_url, self.config.kagent_a2a_timeout)
        
        # Initialize Slack client
        self.slack_timeout = ClientTimeout(total=30)
        
        # WebSocket connection state
        self.websocket = None
        self.is_connected = False
        self.reconnect_attempts = 0
        
    def is_permanent_auth_error(self, error_code: str) -> bool:
        """Check if error is a permanent authentication failure that shouldn't be retried"""
        permanent_errors = {
            'invalid_auth',
            'account_inactive', 
            'invalid_app_id',
            'invalid_client_id',
            'invalid_client_secret',
            'token_revoked',
            'not_authed',
            'missing_scope'
        }
        return error_code in permanent_errors

    async def get_websocket_url(self) -> str:
        """Get WebSocket URL from Slack's Socket Mode API"""
        try:
            url = "https://slack.com/api/apps.connections.open"
            headers = {
                'Authorization': f'Bearer {self.app_token}',
                'Content-Type': 'application/json'
            }
            
            async with aiohttp.ClientSession(timeout=self.slack_timeout) as session:
                async with session.post(url, headers=headers) as response:
                    result = await response.json()
                    
                    if not result.get('ok'):
                        error_code = result.get('error', 'unknown')
                        if self.is_permanent_auth_error(error_code):
                            logger.error(
                                "PERMANENT AUTHENTICATION FAILURE - Bot will not retry",
                                error=error_code,
                                app_token_prefix=self.app_token[:12] + "..." if self.app_token else "None",
                                help_message="Please check your SLACK_APP_TOKEN and SLACK_BOT_TOKEN environment variables"
                            )
                            raise ValueError(f"Permanent authentication failure: {error_code}")
                        else:
                            raise Exception(f"Failed to get WebSocket URL: {error_code}")
                    
                    return result['url']
                    
        except ValueError:
            # Re-raise permanent auth errors without modification
            raise
        except Exception as e:
            logger.error("Failed to get WebSocket URL", error=str(e))
            raise

    async def send_slack_message(self, channel: str, text: str, thread_ts: Optional[str] = None) -> Dict[str, Any]:
        """Send message to Slack channel using Web API"""
        try:
            url = "https://slack.com/api/chat.postMessage"
            
            payload = {
                "channel": channel,
                "text": InputValidator.sanitize_text(text, 3000),
                "as_user": True
            }
            
            if thread_ts:
                payload["thread_ts"] = thread_ts
            
            headers = {
                'Authorization': f'Bearer {self.bot_token}',
                'Content-Type': 'application/json',
                'User-Agent': 'SlackBot-SocketMode/1.0'
            }
            
            async with aiohttp.ClientSession(timeout=self.slack_timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    result = await response.json()
                    
                    if not result.get('ok'):
                        raise Exception(f"Slack API error: {result.get('error')}")
                    
                    return result
                    
        except Exception as e:
            logger.error("Failed to send Slack message", 
                        channel=channel, 
                        error=str(e))
            raise

    async def acknowledge_message(self, envelope_id: str):
        """Acknowledge a Socket Mode message"""
        try:
            if self.websocket:
                ack_message = {
                    "envelope_id": envelope_id
                }
                await self.websocket.send(json.dumps(ack_message))
                logger.debug("Acknowledged message", envelope_id=envelope_id)
        except Exception as e:
            logger.error("Failed to acknowledge message", 
                        envelope_id=envelope_id, 
                        error=str(e))

    async def process_socket_message(self, message: str):
        """Process incoming Socket Mode message"""
        start_time = time.time()
        
        try:
            data = json.loads(message)
            message_type = data.get('type')
            
            logger.debug("Received message", type=message_type)
            WEBSOCKET_MESSAGES.labels(type=message_type, status='received').inc()
            
            # Validate message structure
            is_valid, error_msg = self.validator.validate_socket_mode_payload(data)
            if not is_valid:
                logger.warning("Invalid message structure", error=error_msg)
                WEBSOCKET_MESSAGES.labels(type=message_type, status='invalid').inc()
                return
            
            # Handle different message types
            if message_type == 'hello':
                await self.handle_hello_message(data)
            elif message_type == 'events_api':
                await self.handle_events_api_message(data)
            elif message_type == 'disconnect':
                await self.handle_disconnect_message(data)
            else:
                logger.info("Unhandled message type", type=message_type)
            
            WEBSOCKET_MESSAGES.labels(type=message_type, status='processed').inc()
            
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in WebSocket message", error=str(e))
            WEBSOCKET_MESSAGES.labels(type='unknown', status='json_error').inc()
        except Exception as e:
            logger.error("Error processing WebSocket message", error=str(e))
            WEBSOCKET_MESSAGES.labels(type='unknown', status='error').inc()
        finally:
            duration = time.time() - start_time
            WEBSOCKET_DURATION.observe(duration)

    async def handle_hello_message(self, data: Dict[str, Any]):
        """Handle WebSocket hello message"""
        logger.info("Received hello message from Slack", 
                   connection_info=data.get('connection_info', {}))
        self.is_connected = True
        self.reconnect_attempts = 0

    async def handle_disconnect_message(self, data: Dict[str, Any]):
        """Handle WebSocket disconnect message"""
        logger.warning("Received disconnect message from Slack", 
                      reason=data.get('reason', 'unknown'))
        self.is_connected = False

    async def handle_events_api_message(self, data: Dict[str, Any]):
        """Handle Events API message from Socket Mode"""
        try:
            envelope_id = data.get('envelope_id')
            payload = data.get('payload', {})
            
            # Acknowledge receipt immediately
            if envelope_id:
                await self.acknowledge_message(envelope_id)
            
            # Validate team_id
            if payload.get('team_id') != self.team_id:
                logger.warning("Invalid team_id", 
                             provided_team=payload.get('team_id'))
                return
            
            # Process the event
            event_type = payload.get('type')
            
            if event_type == 'event_callback':
                event = payload.get('event', {})
                await self.process_event(event)
            elif event_type == 'url_verification':
                # URL verification not needed in Socket Mode
                logger.info("URL verification in Socket Mode (ignored)")
            else:
                logger.info("Unhandled event type", event_type=event_type)
                
        except Exception as e:
            logger.error("Error handling events API message", error=str(e))

    async def process_event(self, event: Dict[str, Any]):
        """Process Slack event and interact with kagent"""
        try:
            event_type = event.get('type')
            
            if event_type == 'message':
                # Handle message events
                channel = event.get('channel')
                user = event.get('user')
                text = event.get('text', '')
                ts = event.get('ts')
                
                # Ignore bot messages
                if event.get('bot_id') or event.get('subtype') == 'bot_message':
                    return
                
                # Check if channel is allowed
                if channel not in self.channel_ids:
                    logger.debug("Message from unauthorized channel", channel=channel)
                    return
                
                # Rate limiting per user
                if not await self.rate_limiter.is_allowed(user):
                    logger.warning("Rate limit exceeded for user", user=user)
                    await self.send_slack_message(
                        channel=channel,
                        text="Please slow down! You're sending messages too quickly.",
                        thread_ts=ts
                    )
                    return
                
                # Check if message mentions the bot or is a direct message
                if not self.should_respond_to_message(text, channel):
                    return
                
                # Clean and validate message text
                clean_text = self.validator.sanitize_text(text, 2000)
                
                if len(clean_text.strip()) == 0:
                    return
                
                # Extract command from message
                command = self.extract_command(clean_text)
                
                if command:
                    # Invoke kagent agent
                    try:
                        response = await self.a2a_client.invoke_agent(
                            agent_name="k8s-agent",  # Configure as needed
                            task=command,
                            session_id=f"slack-{user}-{channel}"
                        )
                        
                        # Extract response text
                        if response.get('status') == 'completed':
                            result_text = response.get('result', 'Task completed successfully')
                        else:
                            result_text = f"Task status: {response.get('status', 'unknown')}"
                        
                        # Send response back to Slack
                        await self.send_slack_message(
                            channel=channel,
                            text=result_text,
                            thread_ts=ts
                        )
                        
                        logger.info("Successfully processed command", 
                                  command=command, 
                                  channel=channel, 
                                  user=user)
                        
                    except Exception as e:
                        logger.error("Failed to process command", 
                                   command=command, 
                                   error=str(e))
                        
                        # Send error message to user
                        await self.send_slack_message(
                            channel=channel,
                            text="Sorry, I encountered an error processing your request.",
                            thread_ts=ts
                        )
            
        except Exception as e:
            logger.error("Event processing error", 
                        event_type=event.get('type'), 
                        error=str(e))

    def should_respond_to_message(self, text: str, channel: str) -> bool:
        """Determine if bot should respond to message"""
        # Always respond in DMs (channels starting with 'D')
        if channel.startswith('D'):
            return True
        
        # Respond if mentioned (check configured keywords)
        text_lower = text.lower()
        
        return any(keyword.lower() in text_lower for keyword in self.bot_keywords)
    
    def extract_command(self, text: str) -> Optional[str]:
        """Extract command from message text"""
        # Remove bot mentions and clean up
        text = re.sub(r'@\w+', '', text).strip()
        text = re.sub(r'\bbot\b|\bkagent\b', '', text, flags=re.IGNORECASE).strip()
        
        if len(text) < 3:  # Too short to be a meaningful command
            return None
        
        return text

    async def connect_websocket(self):
        """Establish WebSocket connection to Slack"""
        try:
            ws_url = await self.get_websocket_url()
            logger.info("Connecting to WebSocket", url=ws_url)
            
            # Create SSL context for secure connections
            ssl_context = ssl.create_default_context()
            
            self.websocket = await websockets.connect(
                ws_url,
                ssl=ssl_context,
                ping_interval=SecurityConfig.PING_INTERVAL,
                ping_timeout=SecurityConfig.PING_TIMEOUT,
                close_timeout=SecurityConfig.WEBSOCKET_TIMEOUT
            )
            
            WEBSOCKET_CONNECTIONS.set(1)
            logger.info("WebSocket connection established")
            
        except Exception as e:
            logger.error("Failed to connect WebSocket", error=str(e))
            CONNECTION_ERRORS.inc()
            raise

    async def disconnect_websocket(self):
        """Disconnect WebSocket connection"""
        try:
            if self.websocket:
                await self.websocket.close()
                self.websocket = None
            
            self.is_connected = False
            WEBSOCKET_CONNECTIONS.set(0)
            logger.info("WebSocket connection closed")
            
        except Exception as e:
            logger.error("Error disconnecting WebSocket", error=str(e))

    async def run_with_reconnection(self):
        """Run the bot with automatic reconnection"""
        while True:
            try:
                await self.connect_websocket()
                
                # Listen for messages
                async for message in self.websocket:
                    await self.process_socket_message(message)
                    
            except ValueError as e:
                # Permanent authentication errors - don't retry
                logger.error("FATAL: Permanent authentication failure", error=str(e))
                logger.error("Bot shutting down - please fix authentication and restart")
                break
                
            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket connection closed", code=e.code, reason=e.reason)
                CONNECTION_ERRORS.inc()
                self.is_connected = False
                
                # Implement exponential backoff for reconnection
                if self.reconnect_attempts < SecurityConfig.MAX_RECONNECT_ATTEMPTS:
                    self.reconnect_attempts += 1
                    delay = SecurityConfig.RECONNECT_DELAY * (2 ** (self.reconnect_attempts - 1))
                    logger.info("Attempting to reconnect", 
                              attempt=self.reconnect_attempts,
                              delay=delay)
                    await asyncio.sleep(delay)
                else:
                    logger.error("Max reconnection attempts reached")
                    break
                    
            except Exception as e:
                logger.error("Unexpected error in WebSocket loop", error=str(e))
                CONNECTION_ERRORS.inc()
                
                # Check if this might be an auth issue by trying to get WebSocket URL
                try:
                    await self.get_websocket_url()
                except ValueError:
                    # Permanent auth error detected
                    break
                except Exception:
                    # Other error, continue with retry logic
                    pass
                
                await asyncio.sleep(SecurityConfig.RECONNECT_DELAY)
                
            finally:
                await self.disconnect_websocket()

    async def health_check(self) -> Dict[str, Any]:
        """Health check endpoint data"""
        return {
            "status": "healthy" if self.is_connected else "unhealthy",
            "websocket_connected": self.is_connected,
            "reconnect_attempts": self.reconnect_attempts,
            "timestamp": datetime.utcnow().isoformat()
        }

# HTTP server for health checks and metrics
from aiohttp import web

class HealthServer:
    """HTTP server for health checks and metrics"""
    
    def __init__(self, bot: SlackSocketModeBot, port: int = 8080):
        self.bot = bot
        self.port = port
        self.app = None
        
    async def health_check(self, request: web.Request) -> web.Response:
        """Health check endpoint"""
        health_data = await self.bot.health_check()
        status = 200 if health_data["status"] == "healthy" else 503
        return web.json_response(health_data, status=status)

    async def readiness_check(self, request: web.Request) -> web.Response:
        """Readiness check endpoint"""
        is_ready = self.bot.is_connected
        status = 200 if is_ready else 503
        return web.Response(text="READY" if is_ready else "NOT_READY", status=status)

    async def metrics_endpoint(self, request: web.Request) -> web.Response:
        """Prometheus metrics endpoint"""
        return web.Response(
            text=prometheus_client.generate_latest(),
            content_type='text/plain; version=0.0.4; charset=utf-8'
        )

    def create_app(self) -> web.Application:
        """Create and configure the web application"""
        app = web.Application()
        
        # Routes
        app.router.add_get('/health', self.health_check)
        app.router.add_get('/ready', self.readiness_check)
        app.router.add_get('/metrics', self.metrics_endpoint)
        
        return app

    async def start(self):
        """Start the health server"""
        self.app = self.create_app()
        runner = web.AppRunner(self.app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()
        
        logger.info("Health server started", port=self.port)

async def validate_startup_config():
    """Validate configuration at startup with detailed feedback"""
    print("🔍 Validating Slack Bot Configuration...")
    print("=" * 50)
    
    try:
        # Load configuration
        config = ConfigValidator.load_from_env()
        
        # Validate configuration
        errors, warnings = ConfigValidator.validate_config(config, strict=False)
        
        # Display key configuration info
        print("📋 Configuration Status:")
        print("-" * 25)
        
        if config.app_token:
            print(f"✅ SLACK_APP_TOKEN: {config.app_token[:12]}...")
        else:
            print("❌ SLACK_APP_TOKEN: Not set")
            
        if config.bot_token:
            print(f"✅ SLACK_BOT_TOKEN: {config.bot_token[:12]}...")
        else:
            print("❌ SLACK_BOT_TOKEN: Not set")
            
        if config.team_id:
            print(f"✅ SLACK_TEAM_ID: {config.team_id}")
        else:
            print("❌ SLACK_TEAM_ID: Not set")
            
        if config.channel_ids:
            print(f"✅ SLACK_CHANNEL_IDS: {len(config.channel_ids)} channel(s)")
        else:
            print("⚠️  SLACK_CHANNEL_IDS: Not set (bot will work in all channels)")
            
        print(f"✅ BOT_KEYWORDS: {', '.join(config.bot_keywords)}")
        print(f"✅ KAGENT_A2A_URL: {config.kagent_a2a_url}")
        
        # Show errors and warnings
        if errors:
            print("\n🔴 CONFIGURATION ERRORS:")
            for error in errors:
                print(f"  ❌ {error}")
            print("\n💡 Please set the required environment variables:")
            print("  export SLACK_APP_TOKEN=xapp-your-app-token")
            print("  export SLACK_BOT_TOKEN=xoxb-your-bot-token")
            print("  export SLACK_TEAM_ID=T1234567890")
            print("  export SLACK_CHANNEL_IDS=C1234567890,C0987654321")
            return False
        
        if warnings:
            print("\n🟡 CONFIGURATION WARNINGS:")
            for warning in warnings:
                print(f"  ⚠️  {warning}")
        
        # Test Slack API connectivity
        print("\n🌐 Testing Slack API connectivity...")
        success, error_msg = await ConfigValidator.test_slack_connectivity(config)
        
        if success:
            print("✅ Slack API connection successful!")
        else:
            print(f"❌ Slack API connection failed: {error_msg}")
            if "invalid_auth" in str(error_msg).lower() or "authentication" in str(error_msg).lower():
                print("💡 Please check your tokens are correct and have proper permissions")
                return False
            else:
                print("⚠️  Proceeding anyway - might be a temporary network issue")
        
        print(f"\n🎉 Configuration validated successfully!")
        print("=" * 50)
        return True
        
    except ConfigError as e:
        print(f"\n❌ Configuration Error: {e}")
        return False
    except Exception as e:
        print(f"\n❌ Validation failed: {e}")
        return False

async def main():
    """Main function to run the bot"""
    # Configure structured logging
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    try:
        # Validate configuration first
        if not await validate_startup_config():
            logger.error("Configuration validation failed - bot cannot start")
            return
        
        # Initialize bot
        bot = SlackSocketModeBot()
        
        # Initialize health server
        health_port = int(os.getenv('HEALTH_PORT', '8080'))
        health_server = HealthServer(bot, health_port)
        
        # Start health server
        await health_server.start()
        
        logger.info("Starting Slack Socket Mode bot")
        
        # Run bot with reconnection
        await bot.run_with_reconnection()
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error("Fatal error", error=str(e))
        raise

if __name__ == '__main__':
    asyncio.run(main())
