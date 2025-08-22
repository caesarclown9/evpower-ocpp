"""
API эндпоинты для работы с локациями и их статусами
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
import logging

from app.db.session import get_db
from app.services.location_status_service import LocationStatusService

logger = logging.getLogger(__name__)
router = APIRouter()


class LocationCoordinates(BaseModel):
    latitude: Optional[float]
    longitude: Optional[float]


class StationsSummary(BaseModel):
    total: int
    available: int
    occupied: int
    offline: int
    maintenance: int


class ConnectorsSummary(BaseModel):
    total: int
    available: int
    occupied: int
    faulted: int


class StationTariff(BaseModel):
    price_per_kwh: float
    session_fee: float
    currency: str


class StationInfo(BaseModel):
    id: str
    serial_number: str
    model: str
    manufacturer: str
    status: str
    power_capacity: float
    connectors_count: int
    tariff: StationTariff
    connectors_summary: dict


class LocationResponse(BaseModel):
    id: str
    name: str
    address: str
    city: Optional[str]
    country: Optional[str]
    coordinates: LocationCoordinates
    status: str
    stations_summary: StationsSummary
    connectors_summary: ConnectorsSummary
    stations: Optional[List[StationInfo]] = None


class LocationsListResponse(BaseModel):
    success: bool
    locations: List[LocationResponse]
    total: int


@router.get("/locations", response_model=LocationsListResponse)
async def get_locations(
    db: Session = Depends(get_db),
    include_stations: bool = Query(False, description="Включить детальную информацию о станциях")
):
    """
    Получить список всех активных локаций с агрегированными статусами
    
    Статусы локаций:
    - available: все станции свободны
    - partial: есть свободные и занятые станции
    - occupied: все станции заняты
    - maintenance: есть станции на обслуживании
    - offline: есть неработающие станции
    """
    try:
        logger.info("Получение списка локаций с статусами")
        
        # Получаем локации с агрегированными статусами
        locations = await LocationStatusService.get_locations_with_status(db)
        
        # Если нужна детальная информация о станциях
        if include_stations:
            for location in locations:
                location_details = await LocationStatusService.get_location_details(
                    db, location["id"]
                )
                if location_details:
                    location["stations"] = location_details.get("stations", [])
        
        return {
            "success": True,
            "locations": locations,
            "total": len(locations)
        }
        
    except Exception as e:
        logger.error(f"Ошибка получения локаций: {e}")
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка сервера при получении локаций"
        )


@router.get("/locations/{location_id}", response_model=LocationResponse)
async def get_location_details(
    location_id: str,
    db: Session = Depends(get_db)
):
    """
    Получить детальную информацию о локации включая все станции
    """
    try:
        logger.info(f"Получение детальной информации о локации {location_id}")
        
        location_details = await LocationStatusService.get_location_details(db, location_id)
        
        if not location_details:
            raise HTTPException(
                status_code=404,
                detail=f"Локация {location_id} не найдена"
            )
        
        return location_details
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка получения информации о локации {location_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка сервера при получении информации о локации"
        )


@router.post("/locations/{location_id}/refresh-status")
async def refresh_location_status(
    location_id: str,
    db: Session = Depends(get_db)
):
    """
    Принудительно обновить статус локации (сбросить кэш)
    """
    try:
        logger.info(f"Принудительное обновление статуса локации {location_id}")
        
        # Инвалидируем кэш
        await LocationStatusService.invalidate_cache(location_id)
        
        # Получаем свежие данные
        location_details = await LocationStatusService.get_location_details(db, location_id)
        
        if not location_details:
            raise HTTPException(
                status_code=404,
                detail=f"Локация {location_id} не найдена"
            )
        
        return {
            "success": True,
            "message": "Статус локации обновлен",
            "location": location_details
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка обновления статуса локации {location_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка сервера при обновлении статуса локации"
        )