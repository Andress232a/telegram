-- Base de datos para la aplicación Telegram
-- Ejecutar en MySQL después de crear la base de datos

-- Tabla para almacenar información de videos
CREATE TABLE IF NOT EXISTS videos (
    video_id VARCHAR(255) PRIMARY KEY,
    chat_id VARCHAR(255) NOT NULL,
    message_id INT NOT NULL,
    file_size BIGINT,
    timestamp DATETIME NOT NULL,
    filename VARCHAR(500),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_chat_message (chat_id, message_id),
    INDEX idx_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Tabla para almacenar configuraciones (opcional, para múltiples usuarios)
CREATE TABLE IF NOT EXISTS configurations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    phone VARCHAR(50) UNIQUE NOT NULL,
    api_id VARCHAR(50) NOT NULL,
    api_hash VARCHAR(255) NOT NULL,
    session_name VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_phone (phone),
    INDEX idx_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Tabla para progreso de subidas (opcional)
CREATE TABLE IF NOT EXISTS upload_progress (
    upload_id VARCHAR(255) PRIMARY KEY,
    phone VARCHAR(50) NOT NULL,
    chat_id VARCHAR(255),
    filename VARCHAR(500),
    file_size BIGINT,
    progress_percent INT DEFAULT 0,
    status VARCHAR(50) DEFAULT 'uploading',
    video_id VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_phone (phone),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;



