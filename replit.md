# Overview

This is a Telegram bot application that helps users brainstorm and generate creative business ideas using AI. The bot conducts a conversational interview to gather information about the user's budget, skills, and available time, then generates personalized micro-business ideas with implementation plans. The system appears to have two implementations: one using OpenAI for AI-powered responses (bot.py) and another using template-based idea generation (main.py).

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Bot Framework
- **Technology**: Python-telegram-bot library (v20.7) for Telegram Bot API integration
- **Rationale**: Provides async/await support and comprehensive Telegram features
- **Implementation**: Uses Application, CommandHandler, and MessageHandler for routing

## AI Integration
- **Provider**: OpenAI API for creative idea generation
- **Purpose**: Powers intelligent brainstorming and personalized business idea recommendations
- **Design Pattern**: Direct API client integration with environment-based configuration

## Conversation Flow
- **Pattern**: State-based conversation management
- **States**: Budget → Skills → Time (sequential interview process)
- **Rationale**: Gathers structured user input to generate relevant, personalized ideas
- **Data Model**: Context-based storage using telegram.ext context objects

## Message Handling
- **Long Message Strategy**: Automatic message splitting at 4096 character limit
- **Split Logic**: Attempts to break at newlines to preserve formatting
- **Fallback**: Hard split at max_length if no natural break point exists

## Idea Generation Approach
- **Hybrid System**: Template-based generation with randomized components
- **Components**: Pre-defined industries, channels, and monetization strategies
- **Customization**: Templates populated with user's budget/skills/time constraints
- **Output**: 3 business ideas with step-by-step plans and monetization details

## Data Persistence
- **Storage**: CSV file-based lead tracking
- **Schema**: timestamp, user_id, username, budget, skills, time
- **Purpose**: Collect user responses for analysis and follow-up
- **Auto-initialization**: Creates leads.csv if not present

## Localization
- **Primary Language**: Russian (Cyrillic text in prompts and responses)
- **Design Decision**: Single-language implementation for target audience
- **Extensibility**: Hardcoded strings could be extracted for multi-language support

# External Dependencies

## Telegram Bot API
- **Purpose**: Core bot platform and user interaction
- **Authentication**: TELEGRAM_BOT_TOKEN environment variable
- **Features Used**: Commands, messages, keyboards, async handlers

## OpenAI API
- **Purpose**: AI-powered idea generation and creative brainstorming
- **Authentication**: OPENAI_API_KEY environment variable
- **Model**: Not specified in code (defaults to client's default model)
- **Usage**: Chat completion for generating personalized business ideas

## Python Libraries
- **python-telegram-bot (20.7)**: Async Telegram bot framework
- **openai**: Official OpenAI Python client
- **Standard Library**: logging, os, csv, random, datetime, asyncio, typing

## Environment Configuration
- **Required Variables**:
  - `OPENAI_API_KEY`: OpenAI API authentication
  - `TELEGRAM_BOT_TOKEN`: Telegram Bot API token
- **Configuration Method**: Environment variables via os.environ
- **Deployment**: Designed for platforms like Replit with environment secrets

## File System
- **leads.csv**: Local file storage for user conversation data
- **Persistence**: File-based append-only log
- **Limitations**: Not suitable for high-volume production use; consider database migration for scale