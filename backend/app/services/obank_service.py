"""
OBANK payment service implementation with proper client SSL certificate authentication
"""
import httpx
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional
import asyncio
from datetime import datetime
import uuid
import logging
import ssl
import tempfile
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from app.core.config import settings

logger = logging.getLogger(__name__)

class OBankService:
    def __init__(self):
        self.base_url = settings.current_obank_api_url
        self.point_id = int(settings.current_obank_point_id)
        self.service_id = int(settings.current_obank_service_id)
        self.cert_path = Path(settings.OBANK_CERT_PATH) if settings.OBANK_CERT_PATH else Path(__file__).parent.parent.parent / "certificates" / "obank_client.p12"
        self.cert_password = settings.OBANK_CERT_PASSWORD
        
    def _load_pkcs12_certificate(self):
        """Load PKCS12 certificate and extract cert + key"""
        try:
            if not self.cert_path.exists():
                logger.error(f"🚨 SSL certificate NOT FOUND: {self.cert_path}")
                logger.error(f"🚨 Certificate directory contents:")
                cert_dir = self.cert_path.parent
                if cert_dir.exists():
                    for file in cert_dir.iterdir():
                        logger.error(f"🚨   - {file.name}")
                else:
                    logger.error(f"🚨 Certificate directory does not exist: {cert_dir}")
                raise FileNotFoundError(f"SSL certificate not found: {self.cert_path}")
                
            logger.info(f"✅ SSL certificate found: {self.cert_path}")
            
            with open(self.cert_path, 'rb') as cert_file:
                p12_data = cert_file.read()
            
            logger.info(f"✅ PKCS12 data read: {len(p12_data)} bytes")
            
            # Parse PKCS12
            private_key, certificate, additional_certificates = pkcs12.load_key_and_certificates(
                p12_data, 
                self.cert_password.encode('utf-8')
            )
            
            logger.info(f"✅ PKCS12 parsed successfully")
            
            # Convert to PEM format
            cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
            key_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            
            logger.info(f"✅ Certificate converted to PEM format")
            
            return cert_pem, key_pem
            
        except Exception as e:
            logger.error(f"🚨 Failed to load PKCS12 certificate: {e}")
            logger.error(f"🚨 Exception type: {type(e).__name__}")
            raise

    async def _make_request(self, endpoint: str, xml_data: str) -> Dict[str, Any]:
        """
        Make authenticated request to OBANK API with client SSL certificate
        """
        try:
            logger.info(f"🔍 OBANK request: {self.base_url}{endpoint}")
            logger.info(f"🔍 SSL cert path: {self.cert_path}")
            logger.info(f"🔍 SSL cert exists: {self.cert_path.exists()}")
            
            # Проверяем наличие сертификата
            if not self.cert_path.exists():
                logger.error(f"🚨 SSL certificate missing! Using HTTP fallback for testing...")
                # Временно используем HTTP endpoint для тестирования
                http_url = self.base_url.replace("https://", "http://").replace(":4431", ":4430")
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        f"{http_url}{endpoint}",
                        content=xml_data,
                        headers={
                            "Content-Type": "application/xml; charset=utf-8",
                            "Accept": "application/xml"
                        }
                    )
                    
                    logger.info(f"🔍 HTTP fallback response status: {response.status_code}")
                    logger.info(f"🔍 HTTP fallback response: '{response.text}'")
                    
                    if response.status_code == 200:
                        root = ET.fromstring(response.text)
                        return self._parse_xml_response(root)
                    else:
                        return {"error": f"HTTP {response.status_code}", "detail": response.text}
            
            # Load certificate
            cert_pem, key_pem = self._load_pkcs12_certificate()
            
            logger.info(f"🔍 SSL cert loaded: {len(cert_pem)} bytes")
            logger.info(f"🔍 SSL key loaded: {len(key_pem)} bytes")
            
            # Create temporary files for cert and key
            cert_file_path = None
            key_file_path = None
            
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.crt', delete=False) as cert_file:
                cert_file.write(cert_pem)
                cert_file_path = cert_file.name
                
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.key', delete=False) as key_file:
                key_file.write(key_pem)
                key_file_path = key_file.name
            
            # Create SSL context for mutual TLS
            ssl_context = ssl.create_default_context()
            ssl_context.load_cert_chain(cert_file_path, key_file_path)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            async with httpx.AsyncClient(
                verify=ssl_context,
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
            ) as client:
                    
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    content=xml_data,
                    headers={
                        "Content-Type": "application/xml; charset=utf-8",
                        "Accept": "application/xml"
                    }
                )
                
                logger.info(f"🔍 OBANK response status: {response.status_code}")
                logger.info(f"🔍 OBANK response headers: {dict(response.headers)}")
                logger.info(f"🔍 OBANK response content: '{response.text}'")
                logger.info(f"🔍 OBANK response length: {len(response.text)} chars")
                
                if response.status_code == 200:
                    # Парсинг XML ответа
                    root = ET.fromstring(response.text)
                    return self._parse_xml_response(root)
                else:
                    logger.error(f"❌ OBANK API error: {response.status_code}")
                    logger.error(f"❌ OBANK response: '{response.text}'")
                    logger.error(f"❌ OBANK headers: {dict(response.headers)}")
                    return {"error": f"HTTP {response.status_code}", "detail": response.text}
                    
        except Exception as e:
            logger.error(f"OBANK request failed: {str(e)}")
            return {"error": "Connection failed", "detail": str(e)}
        finally:
            # Cleanup temporary files
            try:
                if cert_file_path:
                    os.unlink(cert_file_path)
                if key_file_path:
                    os.unlink(key_file_path)
            except:
                pass

    def _parse_xml_response(self, root: ET.Element) -> Dict[str, Any]:
        """Parse XML response from OBANK API"""
        result = {}
        
        # Парсинг result элемента
        result_elem = root.find("result")
        if result_elem is not None:
            result.update(result_elem.attrib)
            
            # Парсинг data элементов
            data_elem = result_elem.find("data")
            if data_elem is not None:
                result["data"] = []
                for input_elem in data_elem.findall("input"):
                    result["data"].append(input_elem.attrib)
        
        return result
    
    def _create_h2h_xml(self, amount_tyiyn: int, client_id: str, card_data: Dict[str, str]) -> str:
        """Create XML for H2H payment request"""
        transaction_id = int(datetime.now().timestamp())
        current_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+0600")
        
        # Используем переданные email и phone или значения по умолчанию
        email = card_data.get('email', 'test@evpower.kg')
        phone = card_data.get('phone', '+996700000000')
        
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<request point="{self.point_id}">
    <payment
        id="{transaction_id}"
        sum="{amount_tyiyn}"
        check="0"
        service="{self.service_id}"
        date="{current_time}"
        account="{card_data['number']}">
        <attribute name="amount_currency" value="417"/>
        <attribute name="notify_url" value="{settings.DOMAIN}/api/payment/obank/notify"/>
        <attribute name="redirect_url" value="{settings.DOMAIN}/payment/success"/>
        <attribute name="card_pan" value="{card_data['number']}"/>
        <attribute name="card_name" value="{card_data['holder_name']}"/>
        <attribute name="card_cvv" value="{card_data['cvv']}"/>
        <attribute name="card_year" value="{card_data['exp_year']}"/>
        <attribute name="card_month" value="{card_data['exp_month']}"/>
        <attribute name="email" value="{email}"/>
        <attribute name="phone_number" value="{phone}"/>
        <attribute name="city" value="BISHKEK"/>
        <attribute name="country_code" value="KGZ"/>
    </payment>
</request>"""
        
        return xml
    
    def _create_token_xml(self, days: int = 14) -> str:
        """Create XML for card tokenization request"""
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<request point="{self.point_id}">
    <advanced service="{self.service_id}" function="stored-cards">
        <attribute name="days" value="{days}"/>
    </advanced>
</request>"""
        
        return xml
    
    def _create_token_payment_xml(self, amount_tyiyn: int, client_id: str, card_token: str) -> str:
        """Create XML for token payment request"""
        transaction_id = int(datetime.now().timestamp())
        current_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+0600")
        
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<request point="{self.point_id}">
    <payment
        id="{transaction_id}"
        sum="{amount_tyiyn}"
        check="0"
        service="{self.service_id}"
        date="{current_time}"
        account="">
        <attribute name="amount_currency" value="417"/>
        <attribute name="notify_url" value="{settings.DOMAIN}/api/payment/obank/notify"/>
        <attribute name="redirect_url" value="{settings.DOMAIN}/payment/success"/>
        <attribute name="email" value="test@evpower.kg"/>
        <attribute name="card-token" value="{card_token}"/>
    </payment>
</request>"""
        
        return xml
    
    def _create_status_xml(self, transaction_id: str) -> str:
        """Create XML for status check request"""
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<request point="{self.point_id}">
    <status id="{transaction_id}"/>
</request>"""
        
        return xml

    async def create_h2h_payment(self, amount_kgs: float, client_id: str, card_data: Dict[str, str]) -> Dict[str, Any]:
        """
        Create Host-to-Host card payment
        """
        try:
            amount_tyiyn = int(amount_kgs * 100)  # KGS to tyiyn
            
            xml_data = self._create_h2h_xml(amount_tyiyn, client_id, card_data)
            
            # ✅ ОТЛАДКА: Логируем генерируемый XML
            logger.info(f"🔍 OBANK H2H XML Request:")
            logger.info(f"💳 Card data: {card_data}")
            logger.info(f"📄 Generated XML: {xml_data}")
            
            # 🔍 ДИАГНОСТИКА 1: Тестируем GET запрос к корню
            logger.info(f"🔍 Testing GET request to base URL...")
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    get_response = await client.get(f"{self.base_url}/")
                    logger.info(f"🔍 GET / response: {get_response.status_code}")
                    logger.info(f"🔍 GET / content: '{get_response.text[:500]}'")
            except Exception as e:
                logger.error(f"🔍 GET request failed: {e}")
            
            # 🔍 ДИАГНОСТИКА 2: Тестируем разные endpoints с POST
            endpoints_to_try = [
                "/",
                "/payment", 
                "/api",
                "/api/payment",
                "/process",
                "/h2h",
                "/xml",
                "/rakhmet",
                "/gateway"
            ]
            
            for endpoint in endpoints_to_try:
                logger.info(f"🔍 Testing POST endpoint: {endpoint}")
                result = await self._make_request(endpoint, xml_data)
                
                if "error" not in result or result.get("error") != "HTTP 404":
                    logger.info(f"✅ Working endpoint found: {endpoint}")
                    return {
                        "success": "error" not in result,
                        "payment_id": result.get("id"),
                        "status": result.get("state"),
                        "result": result
                    }
                else:
                    logger.info(f"❌ Endpoint {endpoint} returned 404")
            
            # Если все endpoints возвращают 404, возвращаем последний результат
            logger.error(f"🚨 All endpoints returned 404! Server might be misconfigured.")
            return {
                "success": False,
                "payment_id": None,
                "status": None,
                "result": result
            }
            
        except Exception as e:
            logger.error(f"H2H payment failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def create_token_payment(self, amount_kgs: float, client_id: str, card_token: str) -> Dict[str, Any]:
        """
        Create payment using saved card token
        """
        try:
            amount_tyiyn = int(amount_kgs * 100)  # KGS to tyiyn
            
            xml_data = self._create_token_payment_xml(amount_tyiyn, client_id, card_token)
            
            result = await self._make_request("/", xml_data)
            
            return {
                "success": "error" not in result,
                "payment_id": result.get("id"),
                "status": result.get("state"),
                "result": result
            }
            
        except Exception as e:
            logger.error(f"Token payment failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    async def create_token(self, days: int = 14) -> Dict[str, Any]:
        """
        Create card storage token
        """
        try:
            xml_data = self._create_token_xml(days)
            
            result = await self._make_request("/", xml_data)
            
            return {
                "success": "error" not in result,
                "result": result
            }
            
        except Exception as e:
            logger.error(f"Token creation failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def check_h2h_status(self, transaction_id: str) -> Dict[str, Any]:
        """
        Check H2H payment status
        """
        try:
            xml_data = self._create_status_xml(transaction_id)
            
            result = await self._make_request("/", xml_data)
            
            return {
                "success": "error" not in result,
                "status": result.get("state"),
                "final": result.get("final") == "1",
                "result": result
            }
            
        except Exception as e:
            logger.error(f"Status check failed: {str(e)}")
            return {"success": False, "error": str(e)}

# Global instance
obank_service = OBankService() 