"""
Сервис динамического ценообразования для EvPower
Обрабатывает все виды тарификации: по энергии, времени, фиксированные платы
"""
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, time, date, timedelta
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import text
from dataclasses import dataclass
import logging
import json
import hashlib
from enum import Enum

logger = logging.getLogger(__name__)


class TariffType(str, Enum):
    PER_KWH = 'per_kwh'
    PER_MINUTE = 'per_minute'
    SESSION_FEE = 'session_fee'
    PARKING_FEE = 'parking_fee'


class DiscountType(str, Enum):
    PERCENT = 'percent'
    FIXED = 'fixed'


@dataclass
class PricingResult:
    """Результат расчета тарифа"""
    rate_per_kwh: Decimal
    rate_per_minute: Decimal
    session_fee: Decimal
    parking_fee_per_minute: Decimal
    currency: str
    active_rule: str
    rule_details: Dict[str, Any]
    time_based: bool
    next_rate_change: Optional[datetime]
    tariff_plan_id: Optional[str]
    rule_id: Optional[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "rate_per_kwh": float(self.rate_per_kwh),
            "rate_per_minute": float(self.rate_per_minute),
            "session_fee": float(self.session_fee),
            "parking_fee_per_minute": float(self.parking_fee_per_minute),
            "currency": self.currency,
            "active_rule": self.active_rule,
            "rule_details": self.rule_details,
            "time_based": self.time_based,
            "next_rate_change": self.next_rate_change.isoformat() if self.next_rate_change else None,
            "tariff_plan_id": self.tariff_plan_id,
            "rule_id": self.rule_id
        }


@dataclass
class SessionCost:
    """Стоимость сессии зарядки"""
    total: Decimal
    breakdown: Dict[str, Decimal]
    base_amount: Decimal
    discount_amount: Decimal
    final_amount: Decimal
    promo_code_applied: Optional[str]


class PricingCache:
    """Кэш для тарифов"""
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Tuple[Any, datetime]] = {}
        self.ttl_seconds = ttl_seconds
    
    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if (datetime.now() - timestamp).total_seconds() < self.ttl_seconds:
                return value
            else:
                del self._cache[key]
        return None
    
    def set(self, key: str, value: Any) -> None:
        self._cache[key] = (value, datetime.now())
    
    def clear(self) -> None:
        self._cache.clear()
    
    def make_key(self, *args) -> str:
        """Создает ключ кэша из аргументов"""
        key_str = json.dumps(args, sort_keys=True, default=str)
        return hashlib.md5(key_str.encode()).hexdigest()


class PricingService:
    """Сервис для расчета динамических тарифов"""
    
    def __init__(self, db: Session, cache_ttl: int = 300):
        self.db = db
        self._cache = PricingCache(cache_ttl)
    
    def calculate_pricing(
        self, 
        station_id: str,
        connector_type: Optional[str] = None,
        power_kw: Optional[float] = None,
        calculation_time: Optional[datetime] = None,
        client_id: Optional[str] = None,
        skip_cache: bool = False
    ) -> PricingResult:
        """
        Рассчитывает актуальные тарифы для станции
        """
        if not calculation_time:
            calculation_time = datetime.now(timezone.utc)
        
        # Проверяем кэш
        if not skip_cache:
            cache_key = self._cache.make_key(
                station_id, connector_type, power_kw, 
                calculation_time.replace(second=0, microsecond=0), client_id
            )
            cached = self._cache.get(cache_key)
            if cached:
                logger.debug(f"Используем кэшированный тариф для станции {station_id}")
                return cached
        
        # Рассчитываем тариф
        result = self._calculate_pricing_internal(
            station_id, connector_type, power_kw, calculation_time, client_id
        )
        
        # Сохраняем в кэш
        if not skip_cache:
            self._cache.set(cache_key, result)
        
        # Сохраняем историю расчета
        self._save_pricing_history(result, station_id, calculation_time)
        
        return result
    
    def _calculate_pricing_internal(
        self,
        station_id: str,
        connector_type: Optional[str],
        power_kw: Optional[float],
        calculation_time: datetime,
        client_id: Optional[str]
    ) -> PricingResult:
        """Внутренний метод расчета тарифов"""
        
        # 1. Получаем данные станции
        station_data = self._get_station_data(station_id)
        if not station_data:
            raise ValueError(f"Станция {station_id} не найдена")
        
        # 2. Проверяем клиентский тариф (VIP, корпоративный)
        if client_id:
            client_pricing = self._get_client_pricing(client_id, station_id, calculation_time)
            if client_pricing:
                logger.info(f"Применяем клиентский тариф для {client_id}")
                return client_pricing
        
        # 3. Проверяем индивидуальные цены станции
        if station_data['price_per_kwh'] and station_data['price_per_kwh'] > 0:
            logger.info(f"Используем индивидуальную цену станции: {station_data['price_per_kwh']} {station_data['currency']}/кВт⋅ч")
            return PricingResult(
                rate_per_kwh=Decimal(str(station_data['price_per_kwh'])),
                rate_per_minute=Decimal('0'),
                session_fee=Decimal(str(station_data['session_fee'] or 0)),
                parking_fee_per_minute=Decimal('0'),
                currency=station_data['currency'],
                active_rule="Индивидуальный тариф станции",
                rule_details={"type": "station_specific", "station_id": station_id},
                time_based=False,
                next_rate_change=None,
                tariff_plan_id=None,
                rule_id=None
            )
        
        # 4. Ищем применимое правило из тарифного плана
        if station_data['tariff_plan_id']:
            rule = self._find_applicable_rule(
                station_data['tariff_plan_id'],
                connector_type,
                power_kw,
                calculation_time
            )
            
            if rule:
                return self._build_pricing_from_rule(rule, calculation_time)
        
        # 5. Возвращаем базовый тариф
        logger.warning(f"Используем базовый тариф для станции {station_id}")
        return self._get_default_pricing()
    
    def _get_station_data(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Получает данные станции"""
        result = self.db.execute(text("""
            SELECT 
                s.id,
                s.price_per_kwh,
                s.session_fee,
                s.currency,
                s.tariff_plan_id,
                tp.name as tariff_plan_name
            FROM stations s
            LEFT JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.id = :station_id
        """), {"station_id": station_id}).fetchone()
        
        if not result:
            return None
        
        return {
            'id': result[0],
            'price_per_kwh': result[1],
            'session_fee': result[2],
            'currency': result[3] or 'KGS',
            'tariff_plan_id': result[4],
            'tariff_plan_name': result[5]
        }
    
    def _get_client_pricing(
        self, 
        client_id: str, 
        station_id: str,
        calculation_time: datetime
    ) -> Optional[PricingResult]:
        """Получает специальный тариф для клиента"""
        result = self.db.execute(text("""
            SELECT 
                ct.tariff_plan_id,
                ct.discount_percent,
                ct.fixed_rate_per_kwh,
                tp.name
            FROM client_tariffs ct
            LEFT JOIN tariff_plans tp ON ct.tariff_plan_id = tp.id
            WHERE ct.client_id = :client_id
                AND ct.is_active = true
                AND ct.valid_from <= :now
                AND (ct.valid_until IS NULL OR ct.valid_until > :now)
            ORDER BY ct.created_at DESC
            LIMIT 1
        """), {
            "client_id": client_id,
            "now": calculation_time
        }).fetchone()
        
        if not result:
            return None
        
        # Если есть фиксированная цена
        if result[2]:
            return PricingResult(
                rate_per_kwh=Decimal(str(result[2])),
                rate_per_minute=Decimal('0'),
                session_fee=Decimal('0'),
                parking_fee_per_minute=Decimal('0'),
                currency='KGS',
                active_rule=f"Специальный тариф клиента",
                rule_details={"type": "client_fixed", "client_id": client_id},
                time_based=False,
                next_rate_change=None,
                tariff_plan_id=result[0],
                rule_id=None
            )
        
        # Если есть тарифный план со скидкой
        if result[0]:
            rule = self._find_applicable_rule(
                result[0], None, None, calculation_time
            )
            if rule:
                pricing = self._build_pricing_from_rule(rule, calculation_time)
                # Применяем скидку
                if result[1]:
                    discount_multiplier = Decimal('1') - (Decimal(str(result[1])) / Decimal('100'))
                    pricing.rate_per_kwh *= discount_multiplier
                    pricing.rate_per_minute *= discount_multiplier
                    pricing.active_rule = f"{pricing.active_rule} (скидка {result[1]}%)"
                return pricing
        
        return None
    
    def _find_applicable_rule(
        self,
        tariff_plan_id: str,
        connector_type: Optional[str],
        power_kw: Optional[float],
        calculation_time: datetime
    ) -> Optional[Dict[str, Any]]:
        """Находит применимое правило тарификации"""
        
        current_date = calculation_time.date()
        current_time = calculation_time.time()
        weekday = calculation_time.isoweekday()  # 1=Пн, 7=Вс
        is_weekend = weekday >= 6
        
        query = text("""
            SELECT 
                id, name, tariff_type, connector_type,
                power_range_min, power_range_max, price, currency,
                time_start, time_end, is_weekend, priority,
                min_duration_minutes, max_duration_minutes,
                valid_from, valid_until, days_of_week
            FROM tariff_rules
            WHERE tariff_plan_id = :tariff_plan_id
                AND is_active = true
                AND (valid_from IS NULL OR valid_from <= :current_date)
                AND (valid_until IS NULL OR valid_until >= :current_date)
                AND (connector_type = 'ALL' OR connector_type = :connector_type OR :connector_type IS NULL)
                AND (power_range_min IS NULL OR power_range_min <= :power_kw OR :power_kw IS NULL)
                AND (power_range_max IS NULL OR power_range_max >= :power_kw OR :power_kw IS NULL)
                AND (
                    days_of_week IS NULL 
                    OR :weekday = ANY(days_of_week)
                    OR (is_weekend = :is_weekend AND is_weekend IS NOT NULL)
                )
            ORDER BY priority DESC, created_at DESC
        """)
        
        results = self.db.execute(query, {
            "tariff_plan_id": tariff_plan_id,
            "current_date": current_date,
            "connector_type": connector_type,
            "power_kw": power_kw or 0,
            "weekday": weekday,
            "is_weekend": is_weekend
        }).fetchall()
        
        # Фильтруем по времени
        for row in results:
            time_start = row[8]
            time_end = row[9]
            
            if self._is_time_in_range(current_time, time_start, time_end):
                return {
                    'id': row[0],
                    'name': row[1],
                    'tariff_type': row[2],
                    'connector_type': row[3],
                    'power_range_min': row[4],
                    'power_range_max': row[5],
                    'price': row[6],
                    'currency': row[7] or 'KGS',
                    'time_start': time_start,
                    'time_end': time_end,
                    'is_weekend': row[10],
                    'priority': row[11],
                    'min_duration': row[12],
                    'max_duration': row[13],
                    'valid_from': row[14],
                    'valid_until': row[15],
                    'days_of_week': row[16]
                }
        
        return None
    
    def _is_time_in_range(
        self, 
        current: time, 
        start: Optional[time], 
        end: Optional[time]
    ) -> bool:
        """Проверяет, находится ли время в диапазоне"""
        if not start or not end:
            return True
        
        if start < end:
            # Обычный диапазон (09:00 - 18:00)
            return start <= current <= end
        else:
            # Диапазон через полночь (22:00 - 06:00)
            return current >= start or current <= end
    
    def _build_pricing_from_rule(
        self, 
        rule: Dict[str, Any], 
        calculation_time: datetime
    ) -> PricingResult:
        """Строит результат из правила"""
        
        # Определяем когда будет следующее изменение тарифа
        next_change = self._calculate_next_rate_change(
            rule, calculation_time, rule.get('tariff_plan_id')
        )
        
        # Формируем описание правила
        rule_description = self._format_rule_description(rule)
        
        # Распределяем цены по типам
        pricing = PricingResult(
            rate_per_kwh=Decimal('0'),
            rate_per_minute=Decimal('0'),
            session_fee=Decimal('0'),
            parking_fee_per_minute=Decimal('0'),
            currency=rule['currency'],
            active_rule=rule_description,
            rule_details=rule,
            time_based=bool(rule.get('time_start') and rule.get('time_end')),
            next_rate_change=next_change,
            tariff_plan_id=rule.get('tariff_plan_id'),
            rule_id=rule['id']
        )
        
        # Устанавливаем цену в зависимости от типа
        price = Decimal(str(rule['price']))
        tariff_type = rule['tariff_type']
        
        if tariff_type == TariffType.PER_KWH:
            pricing.rate_per_kwh = price
        elif tariff_type == TariffType.PER_MINUTE:
            pricing.rate_per_minute = price
        elif tariff_type == TariffType.SESSION_FEE:
            pricing.session_fee = price
        elif tariff_type == TariffType.PARKING_FEE:
            pricing.parking_fee_per_minute = price
        
        return pricing
    
    def _format_rule_description(self, rule: Dict[str, Any]) -> str:
        """Форматирует описание правила"""
        parts = []
        
        if rule.get('name'):
            parts.append(rule['name'])
        
        if rule.get('time_start') and rule.get('time_end'):
            parts.append(f"{rule['time_start'].strftime('%H:%M')}-{rule['time_end'].strftime('%H:%M')}")
        
        if rule.get('days_of_week'):
            days = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
            selected_days = [days[d-1] for d in sorted(rule['days_of_week'])]
            parts.append(f"({','.join(selected_days)})")
        elif rule.get('is_weekend'):
            parts.append("Выходные")
        
        if not parts:
            tariff_names = {
                TariffType.PER_KWH: 'Тариф за энергию',
                TariffType.PER_MINUTE: 'Поминутный тариф',
                TariffType.SESSION_FEE: 'Фиксированная плата',
                TariffType.PARKING_FEE: 'Плата за парковку'
            }
            parts.append(tariff_names.get(rule['tariff_type'], 'Специальный тариф'))
        
        return ' - '.join(parts)
    
    def _calculate_next_rate_change(
        self,
        current_rule: Dict[str, Any],
        calculation_time: datetime,
        tariff_plan_id: Optional[str]
    ) -> Optional[datetime]:
        """Рассчитывает время следующего изменения тарифа"""
        
        if not tariff_plan_id or not current_rule.get('time_end'):
            return None
        
        # Получаем все правила для этого плана
        next_rules = self.db.execute(text("""
            SELECT time_start, days_of_week, is_weekend
            FROM tariff_rules
            WHERE tariff_plan_id = :plan_id
                AND is_active = true
                AND id != :current_id
                AND time_start IS NOT NULL
            ORDER BY time_start
        """), {
            "plan_id": tariff_plan_id,
            "current_id": current_rule['id']
        }).fetchall()
        
        # Находим ближайшее изменение
        current_time = calculation_time.time()
        current_weekday = calculation_time.isoweekday()
        
        candidates = []
        
        # Проверяем конец текущего правила
        if current_rule.get('time_end'):
            end_time = current_rule['time_end']
            if end_time > current_time:
                # Сегодня
                candidates.append(
                    calculation_time.replace(
                        hour=end_time.hour,
                        minute=end_time.minute,
                        second=0,
                        microsecond=0
                    )
                )
            else:
                # Завтра
                candidates.append(
                    (calculation_time + timedelta(days=1)).replace(
                        hour=end_time.hour,
                        minute=end_time.minute,
                        second=0,
                        microsecond=0
                    )
                )
        
        # Проверяем начало других правил
        for rule_start, days_of_week, is_weekend in next_rules:
            if rule_start > current_time:
                # Может быть сегодня
                if self._is_rule_applicable_on_day(current_weekday, days_of_week, is_weekend):
                    candidates.append(
                        calculation_time.replace(
                            hour=rule_start.hour,
                            minute=rule_start.minute,
                            second=0,
                            microsecond=0
                        )
                    )
        
        if candidates:
            return min(candidates)
        
        return None
    
    def _is_rule_applicable_on_day(
        self,
        weekday: int,
        days_of_week: Optional[List[int]],
        is_weekend: Optional[bool]
    ) -> bool:
        """Проверяет применимость правила в конкретный день"""
        if days_of_week:
            return weekday in days_of_week
        if is_weekend is not None:
            return (weekday >= 6) == is_weekend
        return True
    
    def _get_default_pricing(self) -> PricingResult:
        """Возвращает базовый тариф"""
        return PricingResult(
            rate_per_kwh=Decimal('13.5'),
            rate_per_minute=Decimal('0'),
            session_fee=Decimal('0'),
            parking_fee_per_minute=Decimal('0'),
            currency='KGS',
            active_rule='Базовый тариф',
            rule_details={'type': 'default'},
            time_based=False,
            next_rate_change=None,
            tariff_plan_id=None,
            rule_id=None
        )
    
    def _save_pricing_history(
        self,
        pricing: PricingResult,
        station_id: str,
        calculation_time: datetime
    ) -> None:
        """Сохраняет историю расчета тарифа"""
        try:
            self.db.execute(text("""
                INSERT INTO pricing_history (
                    station_id, tariff_plan_id, rule_id,
                    calculation_time, rate_per_kwh, rate_per_minute,
                    session_fee, parking_fee_per_minute, currency,
                    rule_name, rule_details
                ) VALUES (
                    :station_id, :tariff_plan_id, :rule_id,
                    :calculation_time, :rate_per_kwh, :rate_per_minute,
                    :session_fee, :parking_fee, :currency,
                    :rule_name, :rule_details
                )
            """), {
                "station_id": station_id,
                "tariff_plan_id": pricing.tariff_plan_id,
                "rule_id": pricing.rule_id,
                "calculation_time": calculation_time,
                "rate_per_kwh": pricing.rate_per_kwh,
                "rate_per_minute": pricing.rate_per_minute,
                "session_fee": pricing.session_fee,
                "parking_fee": pricing.parking_fee_per_minute,
                "currency": pricing.currency,
                "rule_name": pricing.active_rule,
                "rule_details": json.dumps(pricing.rule_details)
            })
        except Exception as e:
            logger.warning(f"Не удалось сохранить историю тарифа: {e}")
    
    def calculate_session_cost(
        self,
        energy_kwh: float,
        duration_minutes: int,
        pricing: PricingResult,
        promo_code: Optional[str] = None,
        client_id: Optional[str] = None
    ) -> SessionCost:
        """Рассчитывает полную стоимость сессии"""
        
        breakdown = {}
        
        # Базовые расчеты
        if pricing.rate_per_kwh > 0:
            breakdown['energy_cost'] = Decimal(str(energy_kwh)) * pricing.rate_per_kwh
        
        if pricing.rate_per_minute > 0:
            breakdown['time_cost'] = Decimal(str(duration_minutes)) * pricing.rate_per_minute
        
        if pricing.session_fee > 0:
            breakdown['session_fee'] = pricing.session_fee
        
        base_amount = sum(breakdown.values())
        
        # Применяем промо-код
        discount_amount = Decimal('0')
        promo_applied = None
        
        if promo_code:
            discount = self._apply_promo_code(
                promo_code, base_amount, client_id
            )
            if discount:
                discount_amount = discount['amount']
                promo_applied = discount['code']
        
        final_amount = base_amount - discount_amount
        
        return SessionCost(
            total=final_amount,
            breakdown=breakdown,
            base_amount=base_amount,
            discount_amount=discount_amount,
            final_amount=final_amount,
            promo_code_applied=promo_applied
        )
    
    def _apply_promo_code(
        self,
        code: str,
        amount: Decimal,
        client_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Применяет промо-код"""
        
        # Получаем информацию о промо-коде
        promo = self.db.execute(text("""
            SELECT 
                id, discount_type, discount_value,
                max_discount_amount, min_charge_amount,
                usage_limit, usage_count, client_usage_limit
            FROM promo_codes
            WHERE code = :code
                AND is_active = true
                AND valid_from <= NOW()
                AND valid_until >= NOW()
        """), {"code": code}).fetchone()
        
        if not promo:
            return None
        
        promo_id = promo[0]
        
        # Проверяем минимальную сумму
        if promo[4] and amount < Decimal(str(promo[4])):
            return None
        
        # Проверяем общий лимит использований
        if promo[5] and promo[6] >= promo[5]:
            return None
        
        # Проверяем лимит для клиента
        if client_id and promo[7]:
            client_usage = self.db.execute(text("""
                SELECT COUNT(*)
                FROM promo_code_usage
                WHERE promo_code_id = :promo_id
                    AND client_id = :client_id
            """), {
                "promo_id": promo_id,
                "client_id": client_id
            }).scalar()
            
            if client_usage >= promo[7]:
                return None
        
        # Рассчитываем скидку
        if promo[1] == DiscountType.PERCENT:
            discount = amount * (Decimal(str(promo[2])) / Decimal('100'))
            if promo[3]:  # max_discount_amount
                discount = min(discount, Decimal(str(promo[3])))
        else:  # FIXED
            discount = min(Decimal(str(promo[2])), amount)
        
        return {
            'id': promo_id,
            'code': code,
            'amount': discount
        }
    
    def validate_tariff_rule(
        self,
        tariff_plan_id: str,
        rule_data: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Валидирует правило тарификации"""
        
        # Проверяем обязательные поля
        if not rule_data.get('name'):
            return False, "Название правила обязательно"
        
        if not rule_data.get('tariff_type'):
            return False, "Тип тарифа обязателен"
        
        if rule_data.get('price') is None or rule_data['price'] < 0:
            return False, "Цена должна быть неотрицательной"
        
        # Проверяем диапазоны
        if rule_data.get('power_range_min') and rule_data.get('power_range_max'):
            if rule_data['power_range_min'] > rule_data['power_range_max']:
                return False, "Минимальная мощность не может быть больше максимальной"
        
        # Проверяем временные диапазоны
        if rule_data.get('time_start') and rule_data.get('time_end'):
            if rule_data['time_start'] == rule_data['time_end']:
                return False, "Время начала и окончания не могут совпадать"
        
        # Проверяем конфликты с существующими правилами
        conflicts = self._check_rule_conflicts(tariff_plan_id, rule_data)
        if conflicts:
            return False, f"Конфликт с правилом: {conflicts[0]}"
        
        return True, None
    
    def _check_rule_conflicts(
        self,
        tariff_plan_id: str,
        rule_data: Dict[str, Any]
    ) -> List[str]:
        """Проверяет конфликты с существующими правилами"""
        
        conflicts = []
        
        # Получаем существующие правила
        existing = self.db.execute(text("""
            SELECT name, connector_type, time_start, time_end, 
                   days_of_week, is_weekend, priority
            FROM tariff_rules
            WHERE tariff_plan_id = :plan_id
                AND is_active = true
                AND (:rule_id IS NULL OR id != :rule_id)
        """), {
            "plan_id": tariff_plan_id,
            "rule_id": rule_data.get('id')
        }).fetchall()
        
        for rule in existing:
            # Проверяем пересечение по приоритету
            if rule[6] == rule_data.get('priority'):
                # Проверяем пересечение по другим параметрам
                if self._rules_overlap(rule, rule_data):
                    conflicts.append(rule[0])
        
        return conflicts
    
    def _rules_overlap(
        self,
        rule1: tuple,
        rule2: Dict[str, Any]
    ) -> bool:
        """Проверяет пересечение двух правил"""
        
        # Проверяем тип коннектора
        if rule1[1] != 'ALL' and rule2.get('connector_type') != 'ALL':
            if rule1[1] != rule2.get('connector_type'):
                return False
        
        # Проверяем время
        if rule1[2] and rule1[3] and rule2.get('time_start') and rule2.get('time_end'):
            # Проверяем пересечение временных диапазонов
            if not self._time_ranges_overlap(
                rule1[2], rule1[3],
                rule2['time_start'], rule2['time_end']
            ):
                return False
        
        # Проверяем дни недели
        if rule1[4] and rule2.get('days_of_week'):
            if not set(rule1[4]) & set(rule2['days_of_week']):
                return False
        
        return True
    
    def _time_ranges_overlap(
        self,
        start1: time, end1: time,
        start2: time, end2: time
    ) -> bool:
        """Проверяет пересечение временных диапазонов"""
        
        # Обычные диапазоны
        if start1 < end1 and start2 < end2:
            return not (end1 <= start2 or end2 <= start1)
        
        # Один или оба диапазона через полночь
        return True  # Упрощенно считаем что пересекаются
    
    def get_pricing_analytics(
        self,
        station_id: Optional[str] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None
    ) -> Dict[str, Any]:
        """Получает аналитику по применению тарифов"""
        
        if not date_from:
            date_from = date.today() - timedelta(days=30)
        if not date_to:
            date_to = date.today()
        
        # Получаем статистику из истории
        query = text("""
            SELECT 
                COUNT(*) as total_calculations,
                COUNT(DISTINCT station_id) as unique_stations,
                COUNT(DISTINCT rule_id) as unique_rules,
                AVG(rate_per_kwh) as avg_rate_per_kwh,
                MIN(rate_per_kwh) as min_rate_per_kwh,
                MAX(rate_per_kwh) as max_rate_per_kwh
            FROM pricing_history
            WHERE calculation_time BETWEEN :date_from AND :date_to
                AND (:station_id IS NULL OR station_id = :station_id)
        """)
        
        stats = self.db.execute(query, {
            "date_from": date_from,
            "date_to": date_to,
            "station_id": station_id
        }).fetchone()
        
        # Получаем топ правил
        top_rules = self.db.execute(text("""
            SELECT 
                rule_name,
                COUNT(*) as usage_count,
                AVG(rate_per_kwh) as avg_rate
            FROM pricing_history
            WHERE calculation_time BETWEEN :date_from AND :date_to
                AND (:station_id IS NULL OR station_id = :station_id)
            GROUP BY rule_name
            ORDER BY usage_count DESC
            LIMIT 10
        """), {
            "date_from": date_from,
            "date_to": date_to,
            "station_id": station_id
        }).fetchall()
        
        return {
            'period': {
                'from': date_from.isoformat(),
                'to': date_to.isoformat()
            },
            'statistics': {
                'total_calculations': stats[0] or 0,
                'unique_stations': stats[1] or 0,
                'unique_rules': stats[2] or 0,
                'avg_rate_per_kwh': float(stats[3] or 0),
                'min_rate_per_kwh': float(stats[4] or 0),
                'max_rate_per_kwh': float(stats[5] or 0)
            },
            'top_rules': [
                {
                    'name': rule[0],
                    'usage_count': rule[1],
                    'avg_rate': float(rule[2] or 0)
                }
                for rule in top_rules
            ]
        }
    
    def clear_cache(self) -> None:
        """Очищает кэш тарифов"""
        self._cache.clear()
        logger.info("Кэш тарифов очищен")