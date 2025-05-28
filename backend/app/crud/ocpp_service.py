from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from app.db.models.ocpp import (
    OCPPStationStatus, 
    OCPPTransaction, 
    OCPPMeterValue, 
    OCPPAuthorization,
    OCPPConfiguration,
    Station,
    ChargingSession
)
import logging

logger = logging.getLogger(__name__)

class OCPPStationService:
    """Сервис для управления статусом станций OCPP"""
    
    @staticmethod
    def update_station_status(
        db: Session, 
        station_id: str, 
        status: str,
        error_code: str = None,
        info: str = None,
        vendor_id: str = None,
        vendor_error_code: str = None
    ) -> OCPPStationStatus:
        """Обновление статуса станции"""
        station_status = db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
        
        if not station_status:
            station_status = OCPPStationStatus(station_id=station_id)
            db.add(station_status)
        
        station_status.status = status
        station_status.error_code = error_code
        station_status.info = info
        station_status.vendor_id = vendor_id
        station_status.vendor_error_code = vendor_error_code
        station_status.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(station_status)
        return station_status
    
    @staticmethod
    def update_heartbeat(db: Session, station_id: str) -> OCPPStationStatus:
        """Обновление последнего heartbeat"""
        station_status = db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
        
        if not station_status:
            station_status = OCPPStationStatus(station_id=station_id)
            db.add(station_status)
        
        station_status.last_heartbeat = datetime.utcnow()
        station_status.is_online = True
        
        db.commit()
        db.refresh(station_status)
        return station_status
    
    @staticmethod
    def mark_boot_notification_sent(
        db: Session, 
        station_id: str, 
        firmware_version: str = None
    ) -> OCPPStationStatus:
        """Отметка об отправке BootNotification"""
        station_status = db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
        
        if not station_status:
            station_status = OCPPStationStatus(station_id=station_id)
            db.add(station_status)
        
        station_status.boot_notification_sent = True
        station_status.firmware_version = firmware_version
        station_status.is_online = True
        
        db.commit()
        db.refresh(station_status)
        return station_status
    
    @staticmethod
    def get_station_status(db: Session, station_id: str) -> Optional[OCPPStationStatus]:
        """Получение статуса станции"""
        return db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
    
    @staticmethod
    def get_online_stations(db: Session) -> List[OCPPStationStatus]:
        """Получение списка онлайн станций"""
        cutoff_time = datetime.utcnow() - timedelta(minutes=10)
        return db.query(OCPPStationStatus).filter(
            OCPPStationStatus.is_online == True,
            OCPPStationStatus.last_heartbeat >= cutoff_time
        ).all()

class OCPPTransactionService:
    """Сервис для управления транзакциями OCPP"""
    
    @staticmethod
    def start_transaction(
        db: Session,
        station_id: str,
        transaction_id: int,
        connector_id: int,
        id_tag: str,
        meter_start: float,
        timestamp: datetime,
        charging_session_id: str = None
    ) -> OCPPTransaction:
        """Создание новой транзакции"""
        transaction = OCPPTransaction(
            transaction_id=transaction_id,
            station_id=station_id,
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=meter_start,
            start_timestamp=timestamp,
            charging_session_id=charging_session_id,
            status="Started"
        )
        
        db.add(transaction)
        db.commit()
        db.refresh(transaction)
        
        logger.info(f"OCPP Transaction started: {transaction_id} for station {station_id}")
        return transaction
    
    @staticmethod
    def stop_transaction(
        db: Session,
        station_id: str,
        transaction_id: int,
        meter_stop: float,
        timestamp: datetime,
        stop_reason: str = None
    ) -> Optional[OCPPTransaction]:
        """Завершение транзакции"""
        transaction = db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id,
            OCPPTransaction.transaction_id == transaction_id,
            OCPPTransaction.status == "Started"
        ).first()
        
        if not transaction:
            logger.error(f"Transaction {transaction_id} not found for station {station_id}")
            return None
        
        transaction.meter_stop = meter_stop
        transaction.stop_timestamp = timestamp
        transaction.stop_reason = stop_reason
        transaction.status = "Stopped"
        
        db.commit()
        db.refresh(transaction)
        
        logger.info(f"OCPP Transaction stopped: {transaction_id} for station {station_id}")
        return transaction
    
    @staticmethod
    def get_active_transaction(db: Session, station_id: str) -> Optional[OCPPTransaction]:
        """Получение активной транзакции для станции"""
        return db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id,
            OCPPTransaction.status == "Started"
        ).first()
    
    @staticmethod
    def get_transaction(
        db: Session, 
        station_id: str, 
        transaction_id: int
    ) -> Optional[OCPPTransaction]:
        """Получение транзакции по ID"""
        return db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id,
            OCPPTransaction.transaction_id == transaction_id
        ).first()

class OCPPMeterService:
    """Сервис для управления показаниями счетчиков"""
    
    @staticmethod
    def add_meter_values(
        db: Session,
        station_id: str,
        connector_id: int,
        timestamp: datetime,
        sampled_values: List[Dict[str, Any]],
        transaction_id: int = None
    ) -> OCPPMeterValue:
        """Добавление показаний счетчика"""
        
        # Парсим показания
        energy = None
        power = None
        current = None
        voltage = None
        temperature = None
        soc = None
        
        for sample in sampled_values:
            measurand = sample.get('measurand', '')
            value = sample.get('value')
            
            if value is not None:
                try:
                    if measurand == 'Energy.Active.Import.Register':
                        energy = float(value)
                    elif measurand == 'Power.Active.Import':
                        power = float(value)
                    elif measurand == 'Current.Import':
                        current = float(value)
                    elif measurand == 'Voltage':
                        voltage = float(value)
                    elif measurand == 'Temperature':
                        temperature = float(value)
                    elif measurand == 'SoC':
                        soc = float(value)
                except (ValueError, TypeError):
                    continue
        
        meter_value = OCPPMeterValue(
            transaction_id=transaction_id,
            station_id=station_id,
            connector_id=connector_id,
            timestamp=timestamp,
            sampled_values=sampled_values,
            energy_active_import_register=energy,
            power_active_import=power,
            current_import=current,
            voltage=voltage,
            temperature=temperature,
            soc=soc
        )
        
        db.add(meter_value)
        db.commit()
        db.refresh(meter_value)
        
        return meter_value
    
    @staticmethod
    def get_latest_meter_values(
        db: Session, 
        station_id: str, 
        limit: int = 10
    ) -> List[OCPPMeterValue]:
        """Получение последних показаний счетчика"""
        return db.query(OCPPMeterValue).filter(
            OCPPMeterValue.station_id == station_id
        ).order_by(OCPPMeterValue.timestamp.desc()).limit(limit).all()

class OCPPAuthorizationService:
    """Сервис для управления авторизацией RFID/NFC"""
    
    @staticmethod
    def authorize_id_tag(db: Session, id_tag: str) -> Dict[str, str]:
        """Авторизация ID тега"""
        auth = db.query(OCPPAuthorization).filter(
            OCPPAuthorization.id_tag == id_tag
        ).first()
        
        if not auth:
            return {"status": "Invalid"}
        
        # Проверка срока действия
        if auth.expiry_date and auth.expiry_date <= datetime.utcnow():
            return {"status": "Expired"}
        
        return {"status": auth.status}
    
    @staticmethod
    def add_id_tag(
        db: Session,
        id_tag: str,
        status: str = "Accepted",
        user_id: str = None,
        expiry_date: datetime = None
    ) -> OCPPAuthorization:
        """Добавление нового ID тега"""
        auth = OCPPAuthorization(
            id_tag=id_tag,
            status=status,
            user_id=user_id,
            expiry_date=expiry_date
        )
        
        db.add(auth)
        db.commit()
        db.refresh(auth)
        
        return auth
    
    @staticmethod
    def get_user_by_id_tag(db: Session, id_tag: str) -> Optional[str]:
        """Получение user_id по ID тегу"""
        auth = db.query(OCPPAuthorization).filter(
            OCPPAuthorization.id_tag == id_tag,
            OCPPAuthorization.status == "Accepted"
        ).first()
        
        return auth.user_id if auth else None

class OCPPConfigurationService:
    """Сервис для управления конфигурацией станций"""
    
    @staticmethod
    def get_configuration(
        db: Session, 
        station_id: str, 
        key: str = None
    ) -> List[OCPPConfiguration]:
        """Получение конфигурации станции"""
        query = db.query(OCPPConfiguration).filter(
            OCPPConfiguration.station_id == station_id
        )
        
        if key:
            query = query.filter(OCPPConfiguration.key == key)
        
        return query.all()
    
    @staticmethod
    def set_configuration(
        db: Session,
        station_id: str,
        key: str,
        value: str,
        readonly: bool = False
    ) -> OCPPConfiguration:
        """Установка конфигурации станции"""
        config = db.query(OCPPConfiguration).filter(
            OCPPConfiguration.station_id == station_id,
            OCPPConfiguration.key == key
        ).first()
        
        if not config:
            config = OCPPConfiguration(
                station_id=station_id,
                key=key,
                value=value,
                readonly=readonly
            )
            db.add(config)
        else:
            if not config.readonly:
                config.value = value
                config.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(config)
        
        return config 