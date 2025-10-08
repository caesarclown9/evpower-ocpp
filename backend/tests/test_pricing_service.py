"""
Тесты для сервиса динамического ценообразования
"""
import pytest
from datetime import datetime, timezone, time, timedelta
from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch
import json

from app.services.pricing_service import (
    PricingService, PricingResult, SessionCost,
    TariffType, DiscountType, PricingCache
)


class TestPricingCache:
    """Тесты для кэша тарифов"""
    
    def test_cache_set_and_get(self):
        cache = PricingCache(ttl_seconds=60)
        test_data = {"test": "data"}
        
        cache.set("key1", test_data)
        result = cache.get("key1")
        
        assert result == test_data
    
    def test_cache_expiration(self):
        cache = PricingCache(ttl_seconds=0)  # Мгновенное истечение
        cache.set("key1", "data")
        
        # Небольшая задержка
        import time
        time.sleep(0.1)
        
        result = cache.get("key1")
        assert result is None
    
    def test_cache_key_generation(self):
        cache = PricingCache()
        
        key1 = cache.make_key("station1", "type1", 50.0)
        key2 = cache.make_key("station1", "type1", 50.0)
        key3 = cache.make_key("station2", "type1", 50.0)
        
        assert key1 == key2  # Одинаковые параметры
        assert key1 != key3  # Разные параметры


class TestPricingService:
    """Тесты для основного сервиса"""
    
    @pytest.fixture
    def mock_db(self):
        """Мок базы данных"""
        return MagicMock()
    
    @pytest.fixture
    def pricing_service(self, mock_db):
        """Экземпляр сервиса с моком БД"""
        return PricingService(mock_db)
    
    def test_station_specific_pricing(self, pricing_service, mock_db):
        """Тест индивидуального тарифа станции"""
        # Мокаем данные станции с индивидуальной ценой
        mock_db.execute.return_value.fetchone.return_value = (
            "station1",  # id
            15.5,        # price_per_kwh
            2.0,         # session_fee
            "KGS",       # currency
            None,        # tariff_plan_id
            None         # tariff_plan_name
        )
        
        result = pricing_service.calculate_pricing(
            station_id="station1",
            skip_cache=True
        )
        
        assert isinstance(result, PricingResult)
        assert result.rate_per_kwh == Decimal('15.5')
        assert result.session_fee == Decimal('2.0')
        assert result.currency == "KGS"
        assert result.active_rule == "Индивидуальный тариф станции"
        assert result.time_based is False
    
    def test_tariff_plan_pricing(self, pricing_service, mock_db):
        """Тест тарифа из тарифного плана"""
        # Мокаем данные станции с тарифным планом
        mock_db.execute.return_value.fetchone.side_effect = [
            # Первый вызов - данные станции
            ("station1", None, None, "KGS", "plan1", "План 1"),
            # Второй вызов - правило тарифа
            (
                "rule1", "Дневной тариф", "per_kwh", "ALL",
                0, 100, 12.0, "KGS",
                time(9, 0), time(18, 0), False, 100,
                None, None, None, None, None
            )
        ]
        
        # Мокаем fetchall для правил
        mock_db.execute.return_value.fetchall.return_value = [
            (
                "rule1", "Дневной тариф", "per_kwh", "ALL",
                0, 100, 12.0, "KGS",
                time(9, 0), time(18, 0), False, 100,
                None, None, None, None, None
            )
        ]
        
        with patch('app.services.pricing_service.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2024, 1, 15, 14, 0, tzinfo=timezone.utc)
            
            result = pricing_service.calculate_pricing(
                station_id="station1",
                skip_cache=True
            )
        
        assert result.rate_per_kwh == Decimal('12.0')
        assert "Дневной тариф" in result.active_rule
        assert result.time_based is True
    
    def test_weekend_pricing(self, pricing_service, mock_db):
        """Тест тарифа выходного дня"""
        # Мокаем данные для выходного дня
        mock_db.execute.return_value.fetchone.side_effect = [
            ("station1", None, None, "KGS", "plan1", "План 1"),
            (
                "rule_weekend", "Выходной тариф", "per_kwh", "ALL",
                0, 100, 8.0, "KGS",
                None, None, True, 100,  # is_weekend = True
                None, None, None, None, None
            )
        ]
        
        mock_db.execute.return_value.fetchall.return_value = [
            (
                "rule_weekend", "Выходной тариф", "per_kwh", "ALL",
                0, 100, 8.0, "KGS",
                None, None, True, 100,
                None, None, None, None, None
            )
        ]
        
        with patch('app.services.pricing_service.datetime') as mock_datetime:
            # Суббота
            mock_datetime.now.return_value = datetime(2024, 1, 20, 14, 0, tzinfo=timezone.utc)
            mock_datetime.now.return_value.isoweekday.return_value = 6
            
            result = pricing_service.calculate_pricing(
                station_id="station1",
                skip_cache=True
            )
        
        assert result.rate_per_kwh == Decimal('8.0')
        assert "Выходной" in result.active_rule
    
    def test_client_specific_pricing(self, pricing_service, mock_db):
        """Тест клиентского тарифа с скидкой"""
        mock_db.execute.return_value.fetchone.side_effect = [
            # Данные станции
            ("station1", None, None, "KGS", "plan1", "План 1"),
            # Клиентский тариф со скидкой 20%
            ("plan1", 20.0, None, "VIP План"),
            # Правило тарифа
            (
                "rule1", "Базовый", "per_kwh", "ALL",
                0, 100, 10.0, "KGS",
                None, None, None, 100,
                None, None, None, None, None
            )
        ]
        
        mock_db.execute.return_value.fetchall.return_value = [
            (
                "rule1", "Базовый", "per_kwh", "ALL",
                0, 100, 10.0, "KGS",
                None, None, None, 100,
                None, None, None, None, None
            )
        ]
        
        result = pricing_service.calculate_pricing(
            station_id="station1",
            client_id="client1",
            skip_cache=True
        )
        
        # 10.0 * 0.8 = 8.0 (скидка 20%)
        assert result.rate_per_kwh == Decimal('8.0')
        assert "скидка 20" in result.active_rule
    
    def test_time_range_overlap_night(self, pricing_service):
        """Тест проверки временных диапазонов через полночь"""
        service = pricing_service
        
        # Диапазон через полночь (22:00 - 06:00)
        assert service._is_time_in_range(
            time(23, 30),  # 23:30
            time(22, 0),   # начало в 22:00
            time(6, 0)     # конец в 06:00
        ) is True
        
        assert service._is_time_in_range(
            time(5, 30),   # 05:30
            time(22, 0),   # начало в 22:00
            time(6, 0)     # конец в 06:00
        ) is True
        
        assert service._is_time_in_range(
            time(12, 0),   # 12:00
            time(22, 0),   # начало в 22:00
            time(6, 0)     # конец в 06:00
        ) is False
    
    def test_session_cost_calculation(self, pricing_service):
        """Тест расчета стоимости сессии"""
        pricing = PricingResult(
            rate_per_kwh=Decimal('10.0'),
            rate_per_minute=Decimal('0.5'),
            session_fee=Decimal('5.0'),
            parking_fee_per_minute=Decimal('0'),
            currency='KGS',
            active_rule='Тест',
            rule_details={},
            time_based=False,
            next_rate_change=None,
            tariff_plan_id=None,
            rule_id=None
        )
        
        cost = pricing_service.calculate_session_cost(
            energy_kwh=20.0,
            duration_minutes=60,
            pricing=pricing
        )
        
        assert isinstance(cost, SessionCost)
        assert cost.base_amount == Decimal('235.0')  # 20*10 + 60*0.5 + 5
        assert cost.breakdown['energy_cost'] == Decimal('200.0')
        assert cost.breakdown['time_cost'] == Decimal('30.0')
        assert cost.breakdown['session_fee'] == Decimal('5.0')
        assert cost.final_amount == Decimal('235.0')
    
    def test_promo_code_percent_discount(self, pricing_service, mock_db):
        """Тест промо-кода с процентной скидкой"""
        # Мокаем промо-код со скидкой 15%
        mock_db.execute.return_value.fetchone.return_value = (
            "promo1",      # id
            "percent",     # discount_type
            15.0,          # discount_value
            50.0,          # max_discount_amount
            10.0,          # min_charge_amount
            100,           # usage_limit
            5,             # usage_count
            3              # client_usage_limit
        )
        
        # Мокаем использования клиентом
        mock_db.execute.return_value.scalar.return_value = 0
        
        pricing = PricingResult(
            rate_per_kwh=Decimal('10.0'),
            rate_per_minute=Decimal('0'),
            session_fee=Decimal('0'),
            parking_fee_per_minute=Decimal('0'),
            currency='KGS',
            active_rule='Тест',
            rule_details={},
            time_based=False,
            next_rate_change=None,
            tariff_plan_id=None,
            rule_id=None
        )
        
        cost = pricing_service.calculate_session_cost(
            energy_kwh=20.0,
            duration_minutes=0,
            pricing=pricing,
            promo_code="SAVE15",
            client_id="client1"
        )
        
        assert cost.base_amount == Decimal('200.0')
        assert cost.discount_amount == Decimal('30.0')  # 15% от 200
        assert cost.final_amount == Decimal('170.0')
        assert cost.promo_code_applied == "SAVE15"
    
    def test_promo_code_max_discount_limit(self, pricing_service, mock_db):
        """Тест ограничения максимальной скидки"""
        # Промо-код 50% но максимум 100 сом
        mock_db.execute.return_value.fetchone.return_value = (
            "promo2", "percent", 50.0, 100.0, 0, 100, 0, 10
        )
        mock_db.execute.return_value.scalar.return_value = 0
        
        pricing = PricingResult(
            rate_per_kwh=Decimal('10.0'),
            rate_per_minute=Decimal('0'),
            session_fee=Decimal('0'),
            parking_fee_per_minute=Decimal('0'),
            currency='KGS',
            active_rule='Тест',
            rule_details={},
            time_based=False,
            next_rate_change=None,
            tariff_plan_id=None,
            rule_id=None
        )
        
        cost = pricing_service.calculate_session_cost(
            energy_kwh=50.0,  # 500 сом
            duration_minutes=0,
            pricing=pricing,
            promo_code="HALF50",
            client_id="client1"
        )
        
        assert cost.base_amount == Decimal('500.0')
        assert cost.discount_amount == Decimal('100.0')  # Ограничено максимумом
        assert cost.final_amount == Decimal('400.0')
    
    def test_rule_validation(self, pricing_service, mock_db):
        """Тест валидации правил тарифов"""
        # Валидное правило
        valid_rule = {
            'name': 'Тестовое правило',
            'tariff_type': 'per_kwh',
            'price': 10.0,
            'power_range_min': 0,
            'power_range_max': 100
        }
        
        mock_db.execute.return_value.fetchall.return_value = []
        
        is_valid, error = pricing_service.validate_tariff_rule("plan1", valid_rule)
        assert is_valid is True
        assert error is None
        
        # Невалидное правило - отрицательная цена
        invalid_rule = {
            'name': 'Тест',
            'tariff_type': 'per_kwh',
            'price': -5.0
        }
        
        is_valid, error = pricing_service.validate_tariff_rule("plan1", invalid_rule)
        assert is_valid is False
        assert "неотрицательной" in error
        
        # Невалидный диапазон мощности
        invalid_range = {
            'name': 'Тест',
            'tariff_type': 'per_kwh',
            'price': 10.0,
            'power_range_min': 100,
            'power_range_max': 50
        }
        
        is_valid, error = pricing_service.validate_tariff_rule("plan1", invalid_range)
        assert is_valid is False
        assert "Минимальная мощность" in error
    
    def test_fallback_to_default_pricing(self, pricing_service, mock_db):
        """Тест возврата к базовому тарифу при ошибке"""
        # Станция без тарифного плана и индивидуальной цены
        mock_db.execute.return_value.fetchone.return_value = (
            "station1", None, None, "KGS", None, None
        )
        
        result = pricing_service.calculate_pricing(
            station_id="station1",
            skip_cache=True
        )
        
        assert result.rate_per_kwh == Decimal('9.0')  # Дефолтная цена
        assert result.active_rule == "Базовый тариф"
        assert result.rule_details['type'] == 'default'
    
    def test_caching_works(self, pricing_service, mock_db):
        """Тест что кэширование работает"""
        mock_db.execute.return_value.fetchone.return_value = (
            "station1", 15.0, 2.0, "KGS", None, None
        )
        
        # Первый вызов - идет в БД
        result1 = pricing_service.calculate_pricing("station1")
        assert mock_db.execute.call_count >= 1
        
        # Сбрасываем счетчик
        mock_db.execute.reset_mock()
        
        # Второй вызов - должен взять из кэша
        result2 = pricing_service.calculate_pricing("station1")
        assert mock_db.execute.call_count == 0  # Не должно быть вызовов БД
        
        # Результаты должны быть идентичны
        assert result1.rate_per_kwh == result2.rate_per_kwh
        assert result1.active_rule == result2.active_rule
    
    def test_analytics_aggregation(self, pricing_service, mock_db):
        """Тест аналитики по тарифам"""
        # Мокаем статистику
        mock_db.execute.return_value.fetchone.return_value = (
            100,    # total_calculations
            5,      # unique_stations
            10,     # unique_rules
            12.5,   # avg_rate_per_kwh
            8.0,    # min_rate_per_kwh
            20.0    # max_rate_per_kwh
        )
        
        # Мокаем топ правил
        mock_db.execute.return_value.fetchall.return_value = [
            ("Дневной тариф", 50, 12.0),
            ("Ночной тариф", 30, 8.0),
            ("Выходной тариф", 20, 10.0)
        ]
        
        analytics = pricing_service.get_pricing_analytics()
        
        assert analytics['statistics']['total_calculations'] == 100
        assert analytics['statistics']['unique_stations'] == 5
        assert analytics['statistics']['avg_rate_per_kwh'] == 12.5
        assert len(analytics['top_rules']) == 3
        assert analytics['top_rules'][0]['name'] == "Дневной тариф"
        assert analytics['top_rules'][0]['usage_count'] == 50


class TestComplexScenarios:
    """Тесты сложных сценариев"""
    
    @pytest.fixture
    def pricing_service(self):
        mock_db = MagicMock()
        return PricingService(mock_db), mock_db
    
    def test_multiple_rules_priority(self, pricing_service):
        """Тест выбора правила по приоритету"""
        service, mock_db = pricing_service
        
        # Мокаем несколько правил с разными приоритетами
        mock_db.execute.return_value.fetchone.return_value = (
            "station1", None, None, "KGS", "plan1", "План 1"
        )
        
        mock_db.execute.return_value.fetchall.return_value = [
            # Высший приоритет
            (
                "rule1", "Приоритетный", "per_kwh", "ALL",
                0, 100, 15.0, "KGS",
                time(0, 0), time(23, 59), None, 200,
                None, None, None, None, None
            ),
            # Низший приоритет
            (
                "rule2", "Обычный", "per_kwh", "ALL",
                0, 100, 10.0, "KGS",
                time(0, 0), time(23, 59), None, 100,
                None, None, None, None, None
            )
        ]
        
        result = service.calculate_pricing("station1", skip_cache=True)
        
        # Должно выбраться правило с высшим приоритетом
        assert result.rate_per_kwh == Decimal('15.0')
        assert "Приоритетный" in result.active_rule
    
    def test_connector_type_filtering(self, pricing_service):
        """Тест фильтрации правил по типу коннектора"""
        service, mock_db = pricing_service
        
        mock_db.execute.return_value.fetchone.return_value = (
            "station1", None, None, "KGS", "plan1", "План 1"
        )
        
        # Правило только для Type2
        mock_db.execute.return_value.fetchall.return_value = []
        
        # Мокаем запрос с фильтрацией по типу коннектора
        def execute_side_effect(query, params):
            result = MagicMock()
            if params.get('connector_type') == 'CCS':
                # Нет правил для CCS
                result.fetchall.return_value = []
            else:
                # Есть правило для Type2
                result.fetchall.return_value = [
                    (
                        "rule1", "Type2 тариф", "per_kwh", "Type2",
                        0, 100, 12.0, "KGS",
                        None, None, None, 100,
                        None, None, None, None, None
                    )
                ]
            return result
        
        mock_db.execute.side_effect = execute_side_effect
        
        # Запрос для Type2
        result = service.calculate_pricing(
            "station1",
            connector_type="Type2",
            skip_cache=True
        )
        assert result.rate_per_kwh == Decimal('12.0')
        
        # Запрос для CCS - должен вернуть дефолтный
        result = service.calculate_pricing(
            "station1",
            connector_type="CCS",
            skip_cache=True
        )
        assert result.rate_per_kwh == Decimal('9.0')  # Дефолтный


if __name__ == "__main__":
    pytest.main([__file__, "-v"])