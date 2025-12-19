import os
import re
import asyncio
import aiohttp
import requests
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import yt_dlp
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO if os.getenv('DEBUG', 'False').lower() != 'true' else logging.DEBUG
)
logger = logging.getLogger(__name__)

# Конфигурация из .env
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле!")

DOWNLOAD_TIMEOUT = int(os.getenv('DOWNLOAD_TIMEOUT', '30'))
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', '50')) * 1024 * 1024  # Конвертируем в байты
CLEANUP_INTERVAL = int(os.getenv('CLEANUP_INTERVAL', '3600'))

# Настройка прокси (если указаны в .env)
PROXY_CONFIG = {}
http_proxy = os.getenv('HTTP_PROXY')
https_proxy = os.getenv('HTTPS_PROXY')

if http_proxy:
    PROXY_CONFIG['http'] = http_proxy
if https_proxy:
    PROXY_CONFIG['https'] = https_proxy

class PinterestDownloader:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        }
        self.session = None
        
    async def create_session(self):
        """Создает aiohttp сессию с прокси если нужно"""
        if not self.session:
            connector = aiohttp.TCPConnector(ssl=False)
            self.session = aiohttp.ClientSession(
                connector=connector,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            )
        return self.session
    
    async def close_session(self):
        """Закрывает aiohttp сессию"""
        if self.session:
            await self.session.close()
            self.session = None
    
    def is_pinterest_url(self, url: str) -> bool:
        """Проверяет, является ли ссылка Pinterest"""
        pinterest_domains = [
            'pinterest.com',
            'pinterest.ru',
            'pin.it',
            'pinterest.co.uk',
            'pinterest.ca',
            'pinterest.fr',
            'pinterest.de',
            'pinterest.jp',
            'pinterest.com.au'
        ]
        
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            
            # Убираем www и другие субдомены
            if domain.startswith('www.'):
                domain = domain[4:]
                
            return any(pinterest_domain in domain for pinterest_domain in pinterest_domains)
        except:
            return False
    
    async def extract_media_urls(self, soup: BeautifulSoup, base_url: str) -> dict:
        """Извлекает URL медиа из HTML страницы Pinterest"""
        media_info = {
            'videos': [],
            'images': [],
            'title': '',
            'description': ''
        }
        
        try:
            # Извлекаем заголовок и описание
            title_tag = soup.find('meta', property='og:title') or soup.find('meta', {'name': 'title'})
            if title_tag:
                media_info['title'] = title_tag.get('content', '')
            
            desc_tag = soup.find('meta', property='og:description') or soup.find('meta', {'name': 'description'})
            if desc_tag:
                media_info['description'] = desc_tag.get('content', '')
            
            # Ищем видео
            # 1. В тегах video
            for video in soup.find_all('video'):
                if video.get('src'):
                    video_url = urljoin(base_url, video['src'])
                    media_info['videos'].append(video_url)
                # Проверяем source внутри video
                for source in video.find_all('source'):
                    if source.get('src'):
                        video_url = urljoin(base_url, source['src'])
                        media_info['videos'].append(video_url)
            
            # 2. В meta-тегах
            for meta in soup.find_all('meta'):
                prop = meta.get('property', '')
                content = meta.get('content', '')
                
                if prop in ['og:video', 'og:video:url', 'og:video:secure_url'] and content:
                    video_url = urljoin(base_url, content)
                    media_info['videos'].append(video_url)
                
                if prop in ['og:image', 'twitter:image', 'pinterest:image'] and content:
                    image_url = urljoin(base_url, content)
                    media_info['images'].append(image_url)
            
            # 3. В JSON-LD
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    import json
                    data = json.loads(script.string)
                    self._extract_from_jsonld(data, media_info, base_url)
                except:
                    continue
            
            # 4. Ищем по классам Pinterest (резервный метод)
            for img in soup.find_all('img', {'src': re.compile(r'\.(jpg|jpeg|png|gif|webp)')}):
                src = img.get('src')
                if src and 'pinimg.com' in src:
                    image_url = urljoin(base_url, src)
                    if image_url not in media_info['images']:
                        media_info['images'].append(image_url)
            
            # Удаляем дубликаты
            media_info['videos'] = list(set(media_info['videos']))
            media_info['images'] = list(set(media_info['images']))
            
        except Exception as e:
            logger.error(f"Ошибка при извлечении медиа: {e}")
        
        return media_info
    
    def _extract_from_jsonld(self, data: dict, media_info: dict, base_url: str):
        """Рекурсивно извлекает медиа из JSON-LD данных"""
        if isinstance(data, dict):
            for key, value in data.items():
                if key in ['contentUrl', 'url', 'image', 'video']:
                    if isinstance(value, str) and value:
                        media_url = urljoin(base_url, value)
                        if value.endswith(('.mp4', '.webm', '.mov', '.avi')):
                            if media_url not in media_info['videos']:
                                media_info['videos'].append(media_url)
                        elif value.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                            if media_url not in media_info['images']:
                                media_info['images'].append(media_url)
                elif isinstance(value, (dict, list)):
                    self._extract_from_jsonld(value, media_info, base_url)
        elif isinstance(data, list):
            for item in data:
                self._extract_from_jsonld(item, media_info, base_url)
    
    async def download_media(self, url: str, media_type: str) -> Tuple[Optional[str], Optional[str]]:
        """Скачивает медиа по URL"""
        try:
            # Создаем временную директорию
            temp_dir = Path('temp')
            temp_dir.mkdir(exist_ok=True)
            
            # Генерируем имя файла
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            extension = '.mp4' if media_type == 'video' else '.jpg'
            filename = f"pinterest_{media_type}_{timestamp}{extension}"
            filepath = temp_dir / filename
            
            # Проверяем размер файла перед скачиванием
            session = await self.create_session()
            
            async with session.head(url, allow_redirects=True) as response:
                if response.status == 200:
                    content_length = response.headers.get('Content-Length')
                    if content_length:
                        file_size = int(content_length)
                        if file_size > MAX_FILE_SIZE:
                            return None, f"Файл слишком большой ({file_size/1024/1024:.1f} MB). Максимум: {MAX_FILE_SIZE/1024/1024} MB"
            
            # Скачиваем файл
            logger.info(f"Начинаю скачивание {media_type}: {url}")
            
            async with session.get(url) as response:
                if response.status == 200:
                    # Проверяем размер по мере скачивания
                    downloaded = 0
                    
                    with open(filepath, 'wb') as f:
                        async for chunk in response.content.iter_chunked(1024*1024):  # 1MB chunks
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if downloaded > MAX_FILE_SIZE:
                                f.close()
                                if filepath.exists():
                                    filepath.unlink()
                                return None, f"Файл превысил максимальный размер ({MAX_FILE_SIZE/1024/1024} MB)"
                    
                    # Проверяем окончательный размер
                    final_size = filepath.stat().st_size
                    if final_size > MAX_FILE_SIZE:
                        filepath.unlink()
                        return None, f"Файл слишком большой ({final_size/1024/1024:.1f} MB)"
                    
                    return str(filepath), None
                else:
                    return None, f"Ошибка загрузки: статус {response.status}"
                
        except asyncio.TimeoutError:
            return None, "Таймаут при загрузке файла"
        except Exception as e:
            logger.error(f"Ошибка при скачивании {url}: {e}")
            return None, f"Ошибка загрузки: {str(e)}"
    
    async def get_pinterest_media(self, url: str) -> Tuple[Optional[str], Optional[str], str]:
        """Основная функция для получения медиа с Pinterest"""
        try:
            if not self.is_pinterest_url(url):
                return None, None, "Это не ссылка Pinterest"
            
            session = await self.create_session()
            
            # Получаем HTML страницы
            async with session.get(url) as response:
                if response.status != 200:
                    return None, None, f"Ошибка доступа к странице: статус {response.status}"
                
                html = await response.text()
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # Извлекаем медиа URL
            media_info = await self.extract_media_urls(soup, url)
            
            # Пытаемся скачать видео (если есть)
            if media_info['videos']:
                for video_url in media_info['videos'][:3]:  # Ограничиваем первые 3 видео
                    filepath, error = await self.download_media(video_url, 'video')
                    if filepath:
                        return filepath, 'video', "Видео успешно загружено!"
            
            # Если видео нет, скачиваем изображение (если есть)
            if media_info['images']:
                for image_url in media_info['images'][:5]:  # Ограничиваем первые 5 изображений
                    filepath, error = await self.download_media(image_url, 'image')
                    if filepath:
                        return filepath, 'image', "Изображение успешно загружено!"
                    elif error:
                        logger.warning(f"Не удалось загрузить изображение {image_url}: {error}")
            
            return None, None, "Не удалось найти доступные медиа для скачивания"
            
        except Exception as e:
            logger.error(f"Ошибка при обработке Pinterest URL {url}: {e}")
            return None, None, f"Ошибка: {str(e)}"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_text = """
    🎉 Добро пожаловать в Pinterest Downloader Bot! 🎉

    Отправьте мне ссылку на пин (pin) с Pinterest, и я скачаю для вас:
    📹 Видео - если оно есть в пине
    📸 Изображение - если видео нет или не скачивается

    Просто скопируйте ссылку из Pinterest и отправьте её мне!

    ⚠️ Ограничения:
    • Максимальный размер файла: {} MB
    • Поддерживаются только публичные пины
    • Некоторые видео могут быть защищены от скачивания

    🚀 Начните, отправив ссылку!
    """.format(MAX_FILE_SIZE // (1024 * 1024))
    
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = """
    📖 Помощь по использованию бота:

    1. Найдите пин на Pinterest который хотите скачать
    2. Скопируйте ссылку из адресной строки браузера
    3. Отправьте ссылку этому боту

    🔗 Примеры ссылок:
    • https://www.pinterest.com/pin/1234567890/
    • https://pin.it/abc123def
    • https://pinterest.ru/pin/1234567890/

    ⚠️ Важно:
    • Ссылка должна быть именно на конкретный пин, а не на доску или профиль
    • Бот работает только с публично доступным контентом
    • Скачивание защищенного контента может быть невозможно

    ❓ Если возникли проблемы:
    • Проверьте, что ссылка правильная
    • Убедитесь, что пин публичный
    • Попробуйте другую ссылку

    📞 Для поддержки: ...
    """
    await update.message.reply_text(help_text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_message = update.message.text.strip()
    
    # Проверяем, похоже ли сообщение на ссылку
    if not (user_message.startswith('http://') or user_message.startswith('https://')):
        await update.message.reply_text("Пожалуйста, отправьте ссылку на пин с Pinterest.")
        return
    
    # Отправляем сообщение о начале обработки
    status_msg = await update.message.reply_text("🔍 Анализирую ссылку...")
    
    try:
        # Создаем загрузчик
        downloader = PinterestDownloader()
        
        # Получаем медиа
        await update.message.chat.send_action(action="typing")
        filepath, media_type, message = await downloader.get_pinterest_media(user_message)
        
        # Закрываем сессию
        await downloader.close_session()
        
        if filepath and media_type:
            # Отправляем медиа пользователю
            await update.message.chat.send_action(action="upload_video" if media_type == 'video' else "upload_photo")
            
            try:
                if media_type == 'video':
                    with open(filepath, 'rb') as f:
                        await update.message.reply_video(
                            video=f,
                            caption="✅ Видео успешно скачано с Pinterest!",
                            supports_streaming=True
                        )
                else:
                    with open(filepath, 'rb') as f:
                        await update.message.reply_photo(
                            photo=f,
                            caption="✅ Изображение успешно скачано с Pinterest!"
                        )
                
                # Удаляем временный файл
                try:
                    os.remove(filepath)
                except:
                    pass
                    
            except Exception as e:
                logger.error(f"Ошибка при отправке файла: {e}")
                await update.message.reply_text(f"⚠️ Файл скачан, но возникла ошибка при отправке: {str(e)}")
        else:
            await update.message.reply_text(f"❌ {message}")
    
    except Exception as e:
        logger.error(f"Ошибка в обработке сообщения: {e}")
        await update.message.reply_text(f"⚠️ Произошла ошибка: {str(e)}")
    
    finally:
        # Удаляем сообщение о статусе
        try:
            await status_msg.delete()
        except:
            pass

async def cleanup_temp_files(context: ContextTypes.DEFAULT_TYPE):
    """Очистка временных файлов"""
    try:
        temp_dir = Path('temp')
        if temp_dir.exists():
            for file in temp_dir.glob('*'):
                try:
                    # Удаляем файлы старше 1 часа
                    if file.stat().st_mtime < (datetime.now().timestamp() - 3600):
                        file.unlink()
                except:
                    continue
    except Exception as e:
        logger.error(f"Ошибка при очистке temp файлов: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка вызвана {update}: {context.error}")
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже."
        )
    except:
        pass

def main():
    """Основная функция запуска бота"""
    # Создаем директорию для временных файлов
    Path('temp').mkdir(exist_ok=True)
    
    # Создаем приложение
    application = Application.builder().token(TOKEN).build()
    
    # Регистрируем обработчики команд
    application.add_handler(CommandHandler('start', start_command))
    application.add_handler(CommandHandler('help', help_command))
    
    # Регистрируем обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Регистрируем обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Настраиваем периодическую очистку временных файлов
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(cleanup_temp_files, interval=CLEANUP_INTERVAL, first=10)
    
    # Запускаем бота
    logger.info("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
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
        
