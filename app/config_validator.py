#!/usr/bin/env python3
"""
Shared configuration validation for Slack bot

This module provides centralized configuration validation that can be used by:
- The main bot application
- The configuration checker utility
- Tests and other utilities
"""

import os
import re
import aiohttp
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

@dataclass
class SlackConfig:
    """Slack bot configuration data class"""
    app_token: str
    bot_token: str
    team_id: str
    channel_ids: List[str]
    bot_keywords: List[str]
    kagent_a2a_url: str
    kagent_a2a_timeout: int

class ConfigError(Exception):
    """Configuration validation error"""
    pass

class ConfigValidator:
    """Centralized configuration validation"""
    
    @staticmethod
    def load_from_env() -> SlackConfig:
        """Load configuration from environment variables"""
        # Load basic config
        app_token = os.getenv('SLACK_APP_TOKEN', '')
        bot_token = os.getenv('SLACK_BOT_TOKEN', '')
        team_id = os.getenv('SLACK_TEAM_ID', '')
        channel_ids_str = os.getenv('SLACK_CHANNEL_IDS', '')
        
        # Parse channel IDs
        channel_ids = [ch.strip() for ch in channel_ids_str.split(',') if ch.strip()]
        
        # Load bot keywords
        bot_keywords_str = os.getenv('BOT_KEYWORDS', '@bot,@kagent,hey bot,hey kagent')
        bot_keywords = [keyword.strip() for keyword in bot_keywords_str.split(',') if keyword.strip()]
        
        # Load kagent config
        kagent_a2a_url = os.getenv('KAGENT_A2A_URL', 'http://kagent.kagent.svc.cluster.local:8083/api/a2a')
        kagent_a2a_timeout = int(os.getenv('KAGENT_A2A_TIMEOUT', '30'))
        
        return SlackConfig(
            app_token=app_token,
            bot_token=bot_token,
            team_id=team_id,
            channel_ids=channel_ids,
            bot_keywords=bot_keywords,
            kagent_a2a_url=kagent_a2a_url,
            kagent_a2a_timeout=kagent_a2a_timeout
        )
    
    @staticmethod
    def validate_config(config: SlackConfig, strict: bool = True) -> Tuple[List[str], List[str]]:
        """
        Validate configuration and return (errors, warnings)
        
        Args:
            config: Configuration to validate
            strict: If True, treat warnings as errors
            
        Returns:
            Tuple of (errors, warnings)
        """
        errors = []
        warnings = []
        
        # Validate required fields
        if not config.app_token:
            errors.append("SLACK_APP_TOKEN is required")
        elif not config.app_token.startswith('xapp-'):
            errors.append(f"SLACK_APP_TOKEN must start with 'xapp-' (got: {config.app_token[:12]}...)")
        
        if not config.bot_token:
            errors.append("SLACK_BOT_TOKEN is required")
        elif not config.bot_token.startswith('xoxb-'):
            errors.append(f"SLACK_BOT_TOKEN must start with 'xoxb-' (got: {config.bot_token[:12]}...)")
        
        if not config.team_id:
            errors.append("SLACK_TEAM_ID is required")
        elif not config.team_id.startswith('T'):
            warnings.append(f"SLACK_TEAM_ID should start with 'T' (got: {config.team_id})")
        
        # Validate bot keywords
        if not config.bot_keywords:
            errors.append("BOT_KEYWORDS cannot be empty")
        
        # Validate channel IDs format
        for channel_id in config.channel_ids:
            if not re.match(r'^[CDG][A-Z0-9]{8,}$', channel_id):
                warnings.append(f"Channel ID format might be invalid: {channel_id}")
        
        # Validate kagent configuration
        if not config.kagent_a2a_url:
            warnings.append("KAGENT_A2A_URL is not set")
        
        if config.kagent_a2a_timeout <= 0:
            warnings.append(f"KAGENT_A2A_TIMEOUT should be positive (got: {config.kagent_a2a_timeout})")
        
        return errors, warnings
    
    @staticmethod
    async def test_slack_connectivity(config: SlackConfig) -> Tuple[bool, Optional[str]]:
        """
        Test Slack API connectivity
        
        Returns:
            Tuple of (success, error_message)
        """
        if not config.app_token or not config.app_token.startswith('xapp-'):
            return False, "Invalid app token"
        
        try:
            url = "https://slack.com/api/apps.connections.open"
            headers = {
                'Authorization': f'Bearer {config.app_token}',
                'Content-Type': 'application/json'
            }
            
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers) as response:
                    result = await response.json()
                    
                    if result.get('ok'):
                        return True, None
                    else:
                        error_code = result.get('error', 'unknown')
                        return False, f"Slack API error: {error_code}"
                        
        except Exception as e:
            return False, f"Connection test failed: {str(e)}"
    
    @staticmethod
    def validate_and_raise(config: SlackConfig, strict: bool = True):
        """
        Validate configuration and raise ConfigError if invalid
        
        Args:
            config: Configuration to validate
            strict: If True, treat warnings as errors
            
        Raises:
            ConfigError: If validation fails
        """
        errors, warnings = ConfigValidator.validate_config(config, strict)
        
        if errors:
            raise ConfigError(f"Configuration errors: {'; '.join(errors)}")
        
        if strict and warnings:
            raise ConfigError(f"Configuration warnings (strict mode): {'; '.join(warnings)}")

def load_and_validate_config(strict: bool = True) -> SlackConfig:
    """
    Convenience function to load and validate configuration
    
    Args:
        strict: If True, treat warnings as errors
        
    Returns:
        Validated SlackConfig
        
    Raises:
        ConfigError: If validation fails
    """
    config = ConfigValidator.load_from_env()
    ConfigValidator.validate_and_raise(config, strict)
    return config
