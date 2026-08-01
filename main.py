import os
import json
import requests
import time
import random
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List
from urllib.parse import urlsplit

from DrissionPage import ChromiumPage
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, MofNCompleteColumn

console = Console()
HEADERS = {
    'Referer': 'https://mangalib.me/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}
DEFAULT_IMG_BASE_URL = "https://img3.cdnlibs.org"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.mangalibdownloader.json')
CHAPTER_FETCH_RETRIES = 3
CHAPTER_FETCH_DELAY = (2.5, 5.0)


@dataclass
class DownloadResult:
    url: str
    path: str
    success: bool
    error: str = ""


@dataclass
class PdfResult:
    volume: str
    path: str
    page_count: int


@dataclass
class CbzResult:
    volume: str
    path: str
    page_count: int


@dataclass
class KccResult:
    volume: str
    profile: str
    output_format: str
    paths: List[str]


@dataclass
class ChapterParseResult:
    index: int
    chapter: dict
    download_tasks: List[tuple]
    attempts: int
    error: str = ""

    @property
    def success(self) -> bool:
        return bool(self.download_tasks)

def get_chapters_list(url):
    with console.status("[bold blue]Получаю полный список глав...[/bold blue]", spinner="dots"):
        page = ChromiumPage()
        page.listen.start('api/manga/') 
        page.get(url)
        
        chapters = []
        for res in page.listen.steps():
            data = res.response.body
            if isinstance(data, str): data = json.loads(data)
            inner = data.get('data', data)
            # Ищем массив, где есть объекты с полем 'number'
            if isinstance(inner, list) and len(inner) > 0 and 'number' in inner[0]:
                chapters = inner
                break
        page.quit()
        return chapters

def get_pages_for_chapter(page, chapter_url: str):
    page.listen.start('api/manga/')
    page.get(chapter_url)
    
    for res in page.listen.steps(timeout=5):
        try:
            data = res.response.body
            if isinstance(data, str): data = json.loads(data)
            inner = data.get('data', data)
            if isinstance(inner, dict) and 'pages' in inner:
                return inner['pages'], inner.get('number', '0')
        except: continue
    return [], "0"

session = requests.Session()
session.headers.update(HEADERS)


def download_file(args):
    url, path = args
    # Если файл уже есть и он не пустой (защита от перезаписи при перезапуске)
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return DownloadResult(url, path, True)
        
    retries = 5
    last_error = "неизвестная ошибка"
    for attempt in range(retries):
        try:
            # Имитация человеческой загрузки (отклонение от ритма)
            time.sleep(random.uniform(0.1, 0.4))
            
            res = session.get(url, timeout=15)
            content_type = res.headers.get('Content-Type', '').lower()
            if res.status_code == 200 and content_type.startswith('image/') and len(res.content) > 1024:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(res.content)
                return DownloadResult(url, path, True)
            elif res.status_code == 429:  # Too Many Requests (лимит)
                last_error = "HTTP 429 (Too Many Requests)"
                time.sleep(2 + attempt)  # Прогрессивный таймаут
            else:
                last_error = (
                    f"HTTP {res.status_code}, Content-Type: "
                    f"{content_type or 'не указан'}, {len(res.content)} байт"
                )
                time.sleep(1)
        except requests.RequestException as error:
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(1)
    return DownloadResult(url, path, False, last_error)


def detect_image_base_url(page) -> str:
    """Определяет CDN по фактически загруженной картинке в читалке."""
    try:
        resource_url = page.run_js("""
            return performance.getEntriesByType('resource')
                .map(entry => entry.name)
                .find(url => url.includes('/chapters/') && !url.includes('/api/')) || null;
        """)
        parsed = urlsplit(resource_url or '')
        if parsed.scheme in ('http', 'https') and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return DEFAULT_IMG_BASE_URL


def build_image_url(url_part: str, image_base_url: str) -> str:
    """Добавляет CDN к относительному пути из API MangaLib."""
    if url_part.startswith(('http://', 'https://')):
        return url_part
    return f"{image_base_url.rstrip('/')}/{url_part.lstrip('/')}"


def number_sort_key(value: str):
    """Сортирует номера томов, глав и страниц как числа, включая дробные."""
    try:
        return 0, Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 1, str(value)


def same_volume(first: str, second: str) -> bool:
    try:
        return Decimal(str(first)) == Decimal(str(second))
    except (InvalidOperation, ValueError):
        return str(first) == str(second)


def collect_volume_images(manga_folder: str, volume: str):
    """Собирает все уже скачанные страницы тома в порядке глав и страниц."""
    chapter_pattern = re.compile(r'^v(?P<volume>[^_]+)_c(?P<chapter>.+)$')
    image_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.avif'}
    chapter_folders = []

    if not os.path.isdir(manga_folder):
        return []

    for folder_name in os.listdir(manga_folder):
        folder_path = os.path.join(manga_folder, folder_name)
        match = chapter_pattern.fullmatch(folder_name)
        if match and os.path.isdir(folder_path) and same_volume(match.group('volume'), volume):
            chapter_folders.append((match.group('chapter'), folder_path))

    chapter_folders.sort(key=lambda item: number_sort_key(item[0]))
    images = []
    for _, folder_path in chapter_folders:
        page_files = [
            file_name
            for file_name in os.listdir(folder_path)
            if os.path.splitext(file_name)[1].lower() in image_extensions
            and os.path.getsize(os.path.join(folder_path, file_name)) > 1024
        ]
        page_files.sort(key=lambda name: number_sort_key(os.path.splitext(name)[0]))
        images.extend(os.path.join(folder_path, file_name) for file_name in page_files)
    return images


def create_volume_pdf(manga_folder: str, volume: str) -> PdfResult:
    """Создаёт один PDF из всех скачанных глав указанного тома."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError as error:
        raise RuntimeError(
            "Для формирования PDF установите зависимости: pip install -r requirements.txt"
        ) from error

    image_paths = collect_volume_images(manga_folder, volume)
    if not image_paths:
        raise RuntimeError(f"Для тома {volume} не найдено скачанных страниц")

    prepared_images = []
    for image_path in image_paths:
        try:
            image = ImageReader(image_path)
            width, height = image.getSize()
            if width <= 0 or height <= 0:
                raise ValueError("нулевой размер изображения")
            prepared_images.append((image_path, width, height))
        except Exception as error:
            raise RuntimeError(f"Не удалось прочитать изображение {image_path}: {error}") from error

    pdf_folder = os.path.join(manga_folder, 'pdf')
    os.makedirs(pdf_folder, exist_ok=True)
    safe_volume = re.sub(r'[^0-9A-Za-z._-]+', '_', str(volume))
    output_path = os.path.join(pdf_folder, f"volume_{safe_volume}.pdf")
    temporary_path = f"{output_path}.part"

    page_width, page_height = A4
    document = canvas.Canvas(temporary_path, pagesize=A4, pageCompression=1)
    document.setTitle(f"{os.path.basename(manga_folder)} - Volume {volume}")
    document.setAuthor("MangaLib Downloader")

    try:
        for image_path, image_width, image_height in prepared_images:
            image = ImageReader(image_path)
            scale = min(page_width / image_width, page_height / image_height)
            draw_width = image_width * scale
            draw_height = image_height * scale
            x = (page_width - draw_width) / 2
            y = (page_height - draw_height) / 2
            document.drawImage(
                image,
                x,
                y,
                width=draw_width,
                height=draw_height,
                preserveAspectRatio=True,
                mask='auto',
            )
            document.showPage()
        document.save()
        os.replace(temporary_path, output_path)
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise

    return PdfResult(str(volume), output_path, len(prepared_images))


def create_volume_pdfs(manga_folder: str, volumes) -> List[PdfResult]:
    """Создаёт PDF для каждого затронутого тома."""
    return [create_volume_pdf(manga_folder, volume) for volume in unique_sorted_volumes(volumes)]


def unique_sorted_volumes(volumes) -> List[str]:
    """Удаляет дубликаты томов и возвращает их в числовом порядке."""
    unique_volumes = []
    for volume in volumes:
        if not any(same_volume(volume, existing) for existing in unique_volumes):
            unique_volumes.append(str(volume))
    unique_volumes.sort(key=number_sort_key)
    return unique_volumes


def image_extension_from_content(image_path: str) -> str:
    """Определяет настоящее расширение изображения по сигнатуре файла."""
    with open(image_path, 'rb') as image_file:
        signature = image_file.read(16)

    if signature.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if signature.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if signature.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if signature.startswith(b'RIFF') and signature[8:12] == b'WEBP':
        return '.webp'
    if len(signature) >= 12 and signature[4:12] in (b'ftypavif', b'ftypavis'):
        return '.avif'
    return os.path.splitext(image_path)[1].lower() or '.img'


def create_volume_cbz(manga_folder: str, volume: str) -> CbzResult:
    """Создаёт CBZ из всех скачанных глав указанного тома."""
    image_paths = collect_volume_images(manga_folder, volume)
    if not image_paths:
        raise RuntimeError(f"Для тома {volume} не найдено скачанных страниц")

    cbz_folder = os.path.join(manga_folder, 'cbz')
    os.makedirs(cbz_folder, exist_ok=True)
    safe_volume = re.sub(r'[^0-9A-Za-z._-]+', '_', str(volume))
    output_path = os.path.join(cbz_folder, f"volume_{safe_volume}.cbz")
    temporary_path = f"{output_path}.part"

    try:
        with zipfile.ZipFile(temporary_path, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.comment = (
                f"{os.path.basename(manga_folder)} - Volume {volume}"
            ).encode('utf-8')
            for page_index, image_path in enumerate(image_paths, start=1):
                chapter_folder = os.path.basename(os.path.dirname(image_path))
                chapter_match = re.fullmatch(r'v[^_]+_c(.+)', chapter_folder)
                chapter_number = chapter_match.group(1) if chapter_match else 'unknown'
                safe_chapter = re.sub(r'[^0-9A-Za-z._-]+', '_', chapter_number)
                page_number = os.path.splitext(os.path.basename(image_path))[0]
                safe_page = re.sub(r'[^0-9A-Za-z._-]+', '_', page_number)
                extension = image_extension_from_content(image_path)
                archive_name = (
                    f"{page_index:06d}_c{safe_chapter}_p{safe_page}{extension}"
                )
                archive.write(image_path, archive_name)
        os.replace(temporary_path, output_path)
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise

    return CbzResult(str(volume), output_path, len(image_paths))


def create_volume_cbzs(manga_folder: str, volumes) -> List[CbzResult]:
    """Создаёт CBZ для каждого затронутого тома."""
    return [create_volume_cbz(manga_folder, volume) for volume in unique_sorted_volumes(volumes)]


def load_config() -> dict:
    """Читает пользовательские настройки, не прерывая работу при битом файле."""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as config_file:
            data = json.load(config_file)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    """Атомарно сохраняет пользовательские настройки рядом со скриптом."""
    temporary_path = f"{CONFIG_PATH}.part"
    with open(temporary_path, 'w', encoding='utf-8') as config_file:
        json.dump(config, config_file, ensure_ascii=False, indent=2)
    os.replace(temporary_path, CONFIG_PATH)


def remember_kcc_profile(profile: str) -> None:
    """Запоминает до десяти последних профилей KCC, не выбирая их автоматически."""
    config = load_config()
    recent_profiles = config.get('kcc_recent_profiles', [])
    if not isinstance(recent_profiles, list):
        recent_profiles = []
    recent_profiles = [item for item in recent_profiles if item != profile]
    config['kcc_recent_profiles'] = [profile] + recent_profiles[:9]
    save_config(config)


def get_kcc_profiles():
    """Получает актуальные профили устройств непосредственно из KCC."""
    try:
        from kindlecomicconverter.image import ProfileData
    except ImportError as error:
        raise RuntimeError(
            "KCC не установлен. Выполните: pip install -r requirements.txt"
        ) from error

    categories = {}
    for profile in ProfileData.ProfilesKindle:
        categories[profile] = 'Kindle'
    for profile in ProfileData.ProfilesKobo:
        categories[profile] = 'Kobo'
    for profile in ProfileData.ProfilesRemarkable:
        categories[profile] = 'reMarkable'
    categories['OTHER'] = 'Другое'

    return {
        profile: (data[0], categories.get(profile, 'Другое'))
        for profile, data in ProfileData.Profiles.items()
    }


def choose_kcc_profile():
    """Всегда просит выбрать устройство, показывая недавние профили первыми."""
    profiles = get_kcc_profiles()
    config = load_config()
    recent_profiles = config.get('kcc_recent_profiles', [])
    if not isinstance(recent_profiles, list):
        recent_profiles = []
    recent_profiles = [profile for profile in recent_profiles if profile in profiles]
    ordered_profiles = recent_profiles + [
        profile for profile in profiles if profile not in recent_profiles
    ]

    table = Table(title="Профили устройств KCC")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Код", style="magenta")
    table.add_column("Устройство", style="green")
    table.add_column("Тип")
    table.add_column("История", justify="center")
    for index, profile in enumerate(ordered_profiles, start=1):
        device_name, category = profiles[profile]
        table.add_row(
            str(index),
            profile,
            device_name,
            category,
            "★" if profile in recent_profiles else "",
        )
    console.print(table)

    selection = Prompt.ask(
        "[bold yellow]Выберите устройство по ID (выбор обязателен)[/bold yellow]",
        choices=[str(index) for index in range(1, len(ordered_profiles) + 1)],
    )
    profile = ordered_profiles[int(selection) - 1]
    remember_kcc_profile(profile)
    return profile, profiles[profile][0], profiles[profile][1]


def choose_kcc_format(category: str) -> str:
    """Предлагает форматы, подходящие выбранному семейству устройств."""
    if category == 'Kindle':
        formats = [
            ('EPUB', 'EPUB — для Send to Kindle, без KindleGen'),
            ('MOBI', 'MOBI — требуется Kindle Previewer/KindleGen'),
            ('CBZ', 'CBZ — для совместимых читалок'),
            ('PDF', 'PDF'),
        ]
    elif category == 'Kobo':
        formats = [
            ('EPUB', 'KEPUB — оптимальный вариант для Kobo'),
            ('CBZ', 'CBZ'),
            ('PDF', 'PDF'),
        ]
    elif category == 'reMarkable':
        formats = [
            ('PDF', 'PDF — оптимальный вариант для reMarkable'),
            ('CBZ', 'CBZ'),
            ('EPUB', 'EPUB'),
        ]
    else:
        formats = [
            ('CBZ', 'CBZ'),
            ('PDF', 'PDF'),
            ('EPUB', 'EPUB'),
        ]

    console.print("\n[bold cyan]Формат результата KCC:[/bold cyan]")
    for index, (_, description) in enumerate(formats, start=1):
        console.print(f"[white]{index}) {description}[/white]")
    selection = Prompt.ask(
        "[bold yellow]Выберите формат[/bold yellow]",
        choices=[str(index) for index in range(1, len(formats) + 1)],
        default='1',
    )
    return formats[int(selection) - 1][0]


def ask_positive_int(prompt: str) -> int:
    while True:
        value = IntPrompt.ask(prompt)
        if value > 0:
            return value
        console.print("[red]Введите целое число больше нуля.[/red]")


def find_kcc_executable() -> str:
    """Находит kcc-c2e в активном виртуальном окружении или PATH."""
    executable_names = ['kcc-c2e.exe', 'kcc-c2e'] if os.name == 'nt' else ['kcc-c2e', 'kcc-c2e.exe']
    executable_folder = os.path.dirname(sys.executable)
    for executable_name in executable_names:
        local_path = os.path.join(executable_folder, executable_name)
        if os.path.isfile(local_path):
            return local_path
        discovered_path = shutil.which(executable_name)
        if discovered_path:
            return discovered_path
    raise RuntimeError(
        "Не найден kcc-c2e. Переустановите зависимости: pip install -r requirements.txt"
    )


def stage_kcc_images(image_paths, target_folder: str) -> None:
    """Создаёт упорядоченный входной каталог тома, по возможности через hard links."""
    os.makedirs(target_folder, exist_ok=True)
    for page_index, image_path in enumerate(image_paths, start=1):
        extension = image_extension_from_content(image_path)
        target_path = os.path.join(target_folder, f"{page_index:06d}{extension}")
        try:
            os.link(image_path, target_path)
        except OSError:
            shutil.copy2(image_path, target_path)


def complete_file_suffix(file_name: str) -> str:
    if file_name.lower().endswith('.kepub.epub'):
        return '.kepub.epub'
    return os.path.splitext(file_name)[1].lower()


def create_volume_with_kcc(
    manga_folder: str,
    volume: str,
    profile: str,
    output_format: str,
    custom_size=None,
) -> KccResult:
    """Оптимизирует один том через официальный CLI Kindle Comic Converter."""
    image_paths = collect_volume_images(manga_folder, volume)
    if not image_paths:
        raise RuntimeError(f"Для тома {volume} не найдено скачанных страниц")

    kcc_executable = find_kcc_executable()
    final_folder = os.path.join(manga_folder, 'kcc')
    os.makedirs(final_folder, exist_ok=True)
    safe_volume = re.sub(r'[^0-9A-Za-z._-]+', '_', str(volume))

    with tempfile.TemporaryDirectory(prefix='mangalib-kcc-') as temporary_folder:
        source_folder = os.path.join(temporary_folder, f"volume_{safe_volume}")
        output_folder = os.path.join(temporary_folder, 'output')
        os.makedirs(output_folder, exist_ok=True)
        stage_kcc_images(image_paths, source_folder)

        command = [
            kcc_executable,
            '--profile', profile,
            '--manga-style',
            '--format', output_format,
            '--output', output_folder,
            '--title', f"{os.path.basename(manga_folder)} — том {volume}",
        ]
        if custom_size:
            command.extend([
                '--customwidth', str(custom_size[0]),
                '--customheight', str(custom_size[1]),
            ])
        command.append(source_folder)

        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"KCC завершился с кодом {completed.returncode} для тома {volume}"
            )

        generated_files = [
            os.path.join(output_folder, file_name)
            for file_name in os.listdir(output_folder)
            if os.path.isfile(os.path.join(output_folder, file_name))
        ]
        generated_files.sort(key=lambda path: number_sort_key(os.path.basename(path)))
        if not generated_files:
            raise RuntimeError(f"KCC не создал выходной файл для тома {volume}")

        final_paths = []
        multiple_files = len(generated_files) > 1
        for file_index, generated_path in enumerate(generated_files, start=1):
            suffix = complete_file_suffix(generated_path)
            part_suffix = f"_part_{file_index:02d}" if multiple_files else ""
            final_path = os.path.join(
                final_folder,
                f"volume_{safe_volume}{part_suffix}{suffix}",
            )
            temporary_path = f"{final_path}.part"
            shutil.copy2(generated_path, temporary_path)
            os.replace(temporary_path, final_path)
            final_paths.append(final_path)

    return KccResult(str(volume), profile, output_format, final_paths)


def create_volumes_with_kcc(
    manga_folder: str,
    volumes,
    profile: str,
    output_format: str,
    custom_size=None,
) -> List[KccResult]:
    return [
        create_volume_with_kcc(
            manga_folder,
            volume,
            profile,
            output_format,
            custom_size,
        )
        for volume in unique_sorted_volumes(volumes)
    ]

def build_reader_url(manga_base_url: str, vol: str, num: str) -> str:
    """Генерирует правильную ссылку на читалку, удаляя '/manga/' из пути"""
    reader_base_url = manga_base_url.replace('/manga/', '/')
    return f"{reader_base_url}/read/v{vol}/c{num}"


def parse_chapter_pages(
    page,
    chapter: dict,
    chapter_index: int,
    manga_base_url: str,
    manga_slug: str,
) -> ChapterParseResult:
    """Собирает ссылки одной главы и повторяет запрос при временной ошибке."""
    volume = str(chapter.get('volume', '0'))
    number = str(chapter.get('number', '0'))
    chapter_url = build_reader_url(manga_base_url, volume, number)
    last_error = "API главы не вернул список страниц"

    for attempt in range(1, CHAPTER_FETCH_RETRIES + 1):
        try:
            pages, _ = get_pages_for_chapter(page, chapter_url)
            if pages:
                image_base_url = detect_image_base_url(page)
                folder = os.path.join('downloads', manga_slug, f"v{volume}_c{number}")
                download_tasks = []

                for page_number, page_data in enumerate(pages, start=1):
                    url_part = page_data.get('url') if isinstance(page_data, dict) else None
                    if not url_part:
                        continue
                    image_url = build_image_url(url_part, image_base_url)
                    extension = os.path.splitext(urlsplit(url_part).path)[1].lstrip('.') or 'jpg'
                    image_path = os.path.join(folder, f"{page_number:03d}.{extension}")
                    download_tasks.append((image_url, image_path))

                if download_tasks:
                    return ChapterParseResult(
                        chapter_index,
                        chapter,
                        download_tasks,
                        attempt,
                    )
                last_error = "в ответе главы нет корректных ссылок на изображения"
            else:
                last_error = "API главы не вернул список страниц"
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"

        if attempt < CHAPTER_FETCH_RETRIES:
            time.sleep(random.uniform(1.5, 3.0) * attempt)

    return ChapterParseResult(
        chapter_index,
        chapter,
        [],
        CHAPTER_FETCH_RETRIES,
        last_error,
    )

def main():
    console.print(Panel.fit("[bold magenta]MANGA-LIB BULK DOWNLOADER[/bold magenta]\n[cyan]v1.7 | PDF, CBZ & KCC[/cyan]", border_style="cyan"))

    manga_url_input = Prompt.ask("[bold yellow]Вставьте ссылку на тайтл[/bold yellow]")
    manga_base_url = manga_url_input.split('?')[0].rstrip('/')
    session.headers.update({'Referer': f"{manga_base_url}/"})
    
    try:
        all_chapters = get_chapters_list(manga_url_input)
        if not all_chapters:
            console.print("[red]Ошибка: Список глав пуст.[/red]")
            return

        # Сортируем: сначала по тому, потом по номеру главы
        all_chapters.sort(key=lambda x: (float(x.get('volume', 0)), float(x.get('number', 0))))

        # Выводим ВСЕ главы
        table = Table(title=f"Найдено глав: {len(all_chapters)}")
        table.add_column("ID (для ввода)", style="cyan", justify="center")
        table.add_column("Том", style="magenta")
        table.add_column("Глава", style="green")
        
        for index, ch in enumerate(all_chapters):
            table.add_row(str(index), str(ch.get('volume', '?')), str(ch.get('number', '?')))
        
        console.print(table)

        selection = Prompt.ask("[bold white]Введите диапазон (напр. 0-10) или номера через запятую (0,2,5)[/bold white]")

        selected_chapters = []
        if '-' in selection:
            start, end = map(int, selection.split('-'))
            selected_chapters = all_chapters[start:end+1]
        else:
            indices = [int(i.strip()) for i in selection.split(',')]
            selected_chapters = [all_chapters[i] for i in indices]

        if not selected_chapters:
            console.print("[red]Не выбрано ни одной главы.[/red]")
            return

        all_download_tasks = []
        manga_slug = manga_base_url.split('/')[-1]
        failed_chapter_parses = []
        
        with console.status("[bold blue]Инициализация браузера для сбора страниц...[/bold blue]", spinner="dots"):
            page = ChromiumPage()

        try:
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn(), TimeRemainingColumn(), console=console) as parse_progress:
                parse_task = parse_progress.add_task(
                    "[blue]Последовательный сбор ссылок...",
                    total=len(selected_chapters),
                )

                for index, chapter in enumerate(selected_chapters):
                    volume = chapter.get('volume', '?')
                    number = chapter.get('number', '?')
                    parse_progress.update(
                        parse_task,
                        description=f"[blue]Сбор ссылок: глава {number} (том {volume})",
                    )
                    result = parse_chapter_pages(
                        page,
                        chapter,
                        index,
                        manga_base_url,
                        manga_slug,
                    )
                    if result.success:
                        all_download_tasks.extend(result.download_tasks)
                    else:
                        failed_chapter_parses.append(result)

                    parse_progress.advance(parse_task)
                    if index < len(selected_chapters) - 1:
                        time.sleep(random.uniform(*CHAPTER_FETCH_DELAY))

            # Синхронизируем состояние бота с реальным браузером,
            # чтобы CDN воспринимал скачивание так, будто это мы читаем с сайта.
            try:
                browser_ua = page.run_js("return navigator.userAgent;")
                session.headers.update({'User-Agent': browser_ua})
                for cookie in page.cookies():
                    session.cookies.set(
                        cookie['name'],
                        cookie['value'],
                        domain=cookie.get('domain', ''),
                    )
            except Exception:
                pass
        finally:
            page.quit()

        if failed_chapter_parses:
            console.print(
                f"\n[bold red]Не удалось получить страницы для "
                f"{len(failed_chapter_parses)} глав после "
                f"{CHAPTER_FETCH_RETRIES} попыток:[/bold red]"
            )
            for result in failed_chapter_parses:
                chapter = result.chapter
                console.print(
                    f"[red]- Том {chapter.get('volume', '?')}, "
                    f"глава {chapter.get('number', '?')}: {result.error}[/red]"
                )

        if not all_download_tasks:
            console.print("[red]Нет страниц для скачивания.[/red]")
            return

        console.print(f"\n[bold yellow]Найдено {len(all_download_tasks)} страниц. Начинаю массовую загрузку...[/bold yellow]")
        
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn(), TimeRemainingColumn(), console=console) as progress:
            task = progress.add_task("[white]Массовая загрузка страниц...", total=len(all_download_tasks))
            # Уменьшили потоки до 12, чтобы не ловить бан (Error 429), и используем сессию
            download_results = []
            with ThreadPoolExecutor(max_workers=12) as executor:
                for result in executor.map(download_file, all_download_tasks):
                    download_results.append(result)
                    progress.advance(task)

        failed_downloads = [result for result in download_results if not result.success]
        if failed_downloads:
            console.print(
                f"\n[bold red]Не удалось скачать "
                f"{len(failed_downloads)} из {len(download_results)} страниц.[/bold red]"
            )
            for result in failed_downloads[:10]:
                console.print(f"[red]- {result.url}: {result.error}[/red]")
            if len(failed_downloads) > 10:
                console.print(f"[red]... и ещё {len(failed_downloads) - 10} ошибок.[/red]")
            return

        console.print(
            f"\n[bold rgb(0,255,0)]🚀 Загружено страниц: "
            f"{len(download_results)}. Папка: downloads/{manga_slug}[/bold rgb(0,255,0)]"
        )

        manga_folder = os.path.join('downloads', manga_slug)
        selected_volumes = [chapter.get('volume') for chapter in selected_chapters]
        console.print("\n[bold cyan]Что сделать после загрузки?[/bold cyan]")
        console.print("[white]1) Ничего не делать[/white]")
        console.print("[white]2) Собрать PDF по томам[/white]")
        console.print("[white]3) Собрать CBZ по томам[/white]")
        console.print("[white]4) Подготовить для электронной книги через KCC[/white]")
        postprocess_action = Prompt.ask(
            "[bold yellow]Выберите действие[/bold yellow]",
            choices=['1', '2', '3', '4'],
            default='1',
        )

        if postprocess_action == '2':
            console.print("\n[bold yellow]Формирую PDF по томам...[/bold yellow]")
            pdf_results = create_volume_pdfs(manga_folder, selected_volumes)
            for pdf_result in pdf_results:
                console.print(
                    f"[bold green]PDF тома {pdf_result.volume}: {pdf_result.path} "
                    f"({pdf_result.page_count} стр.)[/bold green]"
                )
        elif postprocess_action == '3':
            console.print("\n[bold yellow]Формирую CBZ по томам...[/bold yellow]")
            cbz_results = create_volume_cbzs(manga_folder, selected_volumes)
            for cbz_result in cbz_results:
                console.print(
                    f"[bold green]CBZ тома {cbz_result.volume}: {cbz_result.path} "
                    f"({cbz_result.page_count} стр.)[/bold green]"
                )
        elif postprocess_action == '4':
            profile, device_name, category = choose_kcc_profile()
            output_format = choose_kcc_format(category)
            custom_size = None
            if profile == 'OTHER':
                console.print("\n[bold cyan]Размер экрана пользовательского устройства:[/bold cyan]")
                custom_size = (
                    ask_positive_int("[yellow]Ширина в пикселях[/yellow]"),
                    ask_positive_int("[yellow]Высота в пикселях[/yellow]"),
                )

            console.print(
                f"\n[bold yellow]Запускаю KCC: {device_name}, формат {output_format}...[/bold yellow]"
            )
            kcc_results = create_volumes_with_kcc(
                manga_folder,
                selected_volumes,
                profile,
                output_format,
                custom_size,
            )
            for kcc_result in kcc_results:
                for result_path in kcc_result.paths:
                    console.print(
                        f"[bold green]KCC тома {kcc_result.volume}: {result_path}[/bold green]"
                    )
        else:
            console.print("[dim]Дополнительные файлы не создавались.[/dim]")

    except Exception as e:
        console.print(f"[bold red]Ошибка программы:[/bold red] {e}")

if __name__ == "__main__":
    main()
