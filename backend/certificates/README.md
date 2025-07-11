# OBANK SSL Client Certificate Setup

## 🔐 КРИТИЧНО: Клиентский сертификат обязателен!

OBANK API требует mutual TLS authentication с клиентским сертификатом PKCS12.

## 📥 Установка сертификата

### 1. Скачать сертификат
Скачать файл с: https://drive.google.com/file/d/17tjIhkFMK7_F3mVQGlANQHtPj19xKZmu/view?usp=sharing

### 2. Установить сертификат
Поместить скачанный файл как:
```
backend/certificates/obank_client.p12
```

### 3. Параметры сертификата
- **Пароль:** `bPAKhpUlss`
- **Формат:** PKCS12 (.p12)
- **Для:** Тестовый сервер test-rakhmet.dengi.kg:4431

## 🔧 После установки сертификата

Раскомментировать в `obank_service.py`:
```python
# cert=(cert_path, cert_password)  # Раскомментировать когда сертификат будет загружен
```

## ⚠️ Без сертификата
SSL handshake будет неудачным - сервер отклоняет подключения без клиентского сертификата.

## 📋 Тестовые данные

### Параметры API
- **point_id:** 4354
- **service_id:** 1331  
- **URL:** https://test-rakhmet.dengi.kg:4431/external/extended-cert

### Тестовые карты
| Card Pan         | Expiry | CVV | Status |
|------------------|--------|-----|--------|
| 4196720011679734 | 12/29  | 554 | Active |
| 4196720017401067 | 10/29  | 026 | Active |
| 9417018759767213 | 05/26  | 670 | Active | 