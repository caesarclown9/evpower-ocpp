#!/usr/bin/env python3
"""
Монитор OCPP логов в реальном времени
Использование: python monitor_ocpp.py [--station STATION_ID] [--level DEBUG|INFO|WARNING|ERROR]
"""

import argparse
import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Set, Optional

class OCPPLogMonitor:
    """Монитор для отслеживания OCPP логов в реальном времени"""
    
    def __init__(self, station_filter: Optional[str] = None, level_filter: str = "INFO"):
        self.station_filter = station_filter
        self.level_filter = level_filter.upper()
        self.log_file = Path("logs/ocpp_debug.log")
        self.error_file = Path("logs/ocpp_errors.log")
        self.position = 0
        self.error_position = 0
        
        # Создаем папку logs если её нет
        os.makedirs("logs", exist_ok=True)
        
        # Создаем файлы если их нет
        self.log_file.touch(exist_ok=True)
        self.error_file.touch(exist_ok=True)
        
        # Цветовые коды для терминала
        self.colors = {
            'DEBUG': '\033[36m',    # Cyan
            'INFO': '\033[32m',     # Green
            'WARNING': '\033[33m',  # Yellow
            'ERROR': '\033[31m',    # Red
            'RESET': '\033[0m',     # Reset
            'BOLD': '\033[1m',      # Bold
            'STATION': '\033[35m',  # Magenta
        }
        
        # Уровни логирования
        self.levels = {'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40}
        self.min_level = self.levels.get(self.level_filter, 20)
        
    def colorize_log(self, line: str) -> str:
        """Добавляет цветовую подсветку к строке лога"""
        # Определяем уровень лога
        level_match = re.search(r' - (DEBUG|INFO|WARNING|ERROR) - ', line)
        if not level_match:
            return line
            
        level = level_match.group(1)
        
        # Пропускаем если уровень слишком низкий
        if self.levels.get(level, 0) < self.min_level:
            return None
            
        # Фильтр по станции
        if self.station_filter:
            if self.station_filter not in line:
                return None
        
        # Подсвечиваем уровень
        line = line.replace(f' - {level} - ', 
                           f' - {self.colors[level]}{level}{self.colors["RESET"]} - ')
        
        # Подсвечиваем станции
        line = re.sub(r'(EVI-\d+|Station [A-Z0-9-]+)', 
                     f'{self.colors["STATION"]}\\1{self.colors["RESET"]}', line)
        
        # Подсвечиваем IP адреса
        line = re.sub(r'(\d+\.\d+\.\d+\.\d+)', 
                     f'{self.colors["BOLD"]}\\1{self.colors["RESET"]}', line)
        
        # Подсвечиваем эмодзи и важные слова
        line = line.replace('🔌', f'{self.colors["BOLD"]}🔌{self.colors["RESET"]}')
        line = line.replace('🚨', f'{self.colors["ERROR"]}🚨{self.colors["RESET"]}')
        line = line.replace('🔴', f'{self.colors["ERROR"]}🔴{self.colors["RESET"]}')
        line = line.replace('🟢', f'{self.colors["INFO"]}🟢{self.colors["RESET"]}')
        line = line.replace('❌', f'{self.colors["ERROR"]}❌{self.colors["RESET"]}')
        line = line.replace('✅', f'{self.colors["INFO"]}✅{self.colors["RESET"]}')
        line = line.replace('🔍', f'{self.colors["WARNING"]}🔍{self.colors["RESET"]}')
        
        return line
    
    async def monitor_file(self, file_path: Path, position_attr: str):
        """Мониторит один файл лога"""
        while True:
            try:
                if not file_path.exists():
                    await asyncio.sleep(1)
                    continue
                
                current_position = getattr(self, position_attr)
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    f.seek(current_position)
                    new_lines = f.readlines()
                    new_position = f.tell()
                
                if new_lines:
                    for line in new_lines:
                        line = line.strip()
                        if line:
                            colored_line = self.colorize_log(line)
                            if colored_line:
                                timestamp = datetime.now().strftime("%H:%M:%S")
                                print(f"[{timestamp}] {colored_line}")
                    
                    setattr(self, position_attr, new_position)
                
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"Ошибка мониторинга {file_path}: {e}")
                await asyncio.sleep(1)
    
    async def start_monitoring(self):
        """Запускает мониторинг всех файлов логов"""
        print(f"{self.colors['BOLD']}=== OCPP Log Monitor ==={self.colors['RESET']}")
        print(f"Уровень: {self.colors[self.level_filter]}{self.level_filter}{self.colors['RESET']}")
        if self.station_filter:
            print(f"Станция: {self.colors['STATION']}{self.station_filter}{self.colors['RESET']}")
        print(f"Файлы: {self.log_file}, {self.error_file}")
        print("-" * 50)
        
        # Начинаем с конца файлов для мониторинга только новых записей
        if self.log_file.exists():
            self.position = self.log_file.stat().st_size
        if self.error_file.exists():
            self.error_position = self.error_file.stat().st_size
        
        # Запускаем мониторинг обоих файлов параллельно
        await asyncio.gather(
            self.monitor_file(self.log_file, "position"),
            self.monitor_file(self.error_file, "error_position")
        )

def main():
    parser = argparse.ArgumentParser(description="Монитор OCPP логов в реальном времени")
    parser.add_argument("--station", "-s", type=str, help="Фильтр по ID станции (например, EVI-0011)")
    parser.add_argument("--level", "-l", type=str, default="INFO", 
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Минимальный уровень логирования")
    parser.add_argument("--tail", "-t", type=int, default=0,
                       help="Показать последние N строк перед началом мониторинга")
    
    args = parser.parse_args()
    
    monitor = OCPPLogMonitor(args.station, args.level)
    
    # Показываем последние строки если запрошено
    if args.tail > 0:
        try:
            if monitor.log_file.exists():
                with open(monitor.log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    for line in lines[-args.tail:]:
                        line = line.strip()
                        if line:
                            colored_line = monitor.colorize_log(line)
                            if colored_line:
                                print(colored_line)
        except Exception as e:
            print(f"Ошибка чтения истории: {e}")
        
        print("-" * 50)
    
    try:
        asyncio.run(monitor.start_monitoring())
    except KeyboardInterrupt:
        print(f"\n{monitor.colors['BOLD']}Мониторинг остановлен{monitor.colors['RESET']}")

if __name__ == "__main__":
    main() 