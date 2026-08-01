import os
import json
import requests
import time
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List
from urllib.parse import urlsplit

from DrissionPage import ChromiumPage
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, MofNCompleteColumn

console = Console()
HEADERS = {
    'Referer': 'https://mangalib.me/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}
DEFAULT_IMG_BASE_URL = "https://img3.cdnlibs.org"


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

def get_pages_for_chapter(page: ChromiumPage, chapter_url: str):
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


def detect_image_base_url(page: ChromiumPage) -> str:
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
    unique_volumes = []
    for volume in volumes:
        if not any(same_volume(volume, existing) for existing in unique_volumes):
            unique_volumes.append(str(volume))
    unique_volumes.sort(key=number_sort_key)
    return [create_volume_pdf(manga_folder, volume) for volume in unique_volumes]

def build_reader_url(manga_base_url: str, vol: str, num: str) -> str:
    """Генерирует правильную ссылку на читалку, удаляя '/manga/' из пути"""
    reader_base_url = manga_base_url.replace('/manga/', '/')
    return f"{reader_base_url}/read/v{vol}/c{num}"

def main():
    console.print(Panel.fit("[bold magenta]MANGA-LIB BULK DOWNLOADER[/bold magenta]\n[cyan]v1.4 | PDF by Volumes[/cyan]", border_style="cyan"))

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

        all_download_tasks = []
        
        with console.status("[bold blue]Инициализация браузера для сбора страниц...[/bold blue]", spinner="dots"):
            page = ChromiumPage()
            page.listen.start('api/manga/')

        # Очередь для сбора страниц
        chapters_to_parse = selected_chapters.copy()
        missed_chapters = []

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn(), TimeRemainingColumn(), console=console) as parse_progress:
            parse_task = parse_progress.add_task("[blue]Парсинг глав...", total=len(chapters_to_parse))

            while chapters_to_parse:
                ch = chapters_to_parse.pop(0)
                vol = ch.get('volume')
                num = ch.get('number')
                
                # Формируем правильную ссылку для читалки
                chapter_read_url = build_reader_url(manga_base_url, vol, num)
                
                parse_progress.update(parse_task, description=f"[blue]Сбор ссылок: Глава {num} (Том {vol})")
                
                pages, actual_num = get_pages_for_chapter(page, chapter_read_url)
                
                if not pages:
                    parse_progress.console.print(f"[red]Сработал лимит на главе {num}. Глава добавлена в конец очереди на повтор.[/red]")
                    missed_chapters.append(ch)
                    # Если пропустили, делаем чуть большую паузу перед следующим запросом
                    time.sleep(random.uniform(5.0, 8.0))
                else:
                    # Папка: название_манги/vol_X_ch_Y
                    manga_slug = manga_base_url.split('/')[-1]
                    folder = f"downloads/{manga_slug}/v{vol}_c{num}"
                    image_base_url = detect_image_base_url(page)
                    for i, p in enumerate(pages):
                        url_part = p['url']
                        f_url = build_image_url(url_part, image_base_url)
                        ext = url_part.split('.')[-1].split('?')[0] # Очистка расширения
                        all_download_tasks.append((f_url, os.path.join(folder, f"{i+1:03d}.{ext}")))
                    
                    parse_progress.advance(parse_task)
                    time.sleep(random.uniform(2.5, 5.0))

            if not chapters_to_parse and missed_chapters:
                parse_progress.console.print(f"[yellow]Начинаю повторный сбор пропущенных глав ({len(missed_chapters)} шт.)... Нажмите Ctrl+C, если хотите прервать парсинг.[/yellow]")
                chapters_to_parse = missed_chapters.copy()
                missed_chapters.clear()
                # Увеличим общий прогресс бар (можно также пересоздать)
                # Прибавлять к total не нужно, так как advance вызывался только при успешном парсинге
                # Просто ждем чуть дольше перед новым циклом, чтобы блокировка спала
                time.sleep(10.0)

        # Синхронизируем состояние бота с реальным браузером,
        # чтобы CDN воспринимал скачивание так, будто это мы читаем с сайта
        try:
            browser_ua = page.run_js("return navigator.userAgent;")
            session.headers.update({'User-Agent': browser_ua})
            for cookie in page.cookies():
                session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))
        except Exception:
            pass

        page.quit()

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
        console.print("\n[bold yellow]Формирую PDF по томам...[/bold yellow]")
        pdf_results = create_volume_pdfs(manga_folder, selected_volumes)
        for pdf_result in pdf_results:
            console.print(
                f"[bold green]PDF тома {pdf_result.volume}: {pdf_result.path} "
                f"({pdf_result.page_count} стр.)[/bold green]"
            )

    except Exception as e:
        console.print(f"[bold red]Ошибка программы:[/bold red] {e}")

if __name__ == "__main__":
    main()
