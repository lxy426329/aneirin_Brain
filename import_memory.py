import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class ImportEngine:
    def __init__(self, config, bucket_mgr, dehydrator, embedding_engine):
        self.config = config
        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        self.embedding_engine = embedding_engine
        self.is_running = False
        self.current_task = None
        self.progress = 0
        self.total = 0
        self.status_message = ""
    
    async def start(self, raw_content, filename, preserve_raw=False, resume=False):
        self.is_running = True
        self.current_task = filename
        self.progress = 0
        self.status_message = "Starting import..."
        
        try:
            content = raw_content.decode('utf-8') if isinstance(raw_content, bytes) else raw_content
            
            lines = content.split('\n')
            self.total = len(lines)
            
            for i, line in enumerate(lines):
                if not self.is_running:
                    break
                
                line = line.strip()
                if not line:
                    continue
                
                self.progress = int((i + 1) / self.total * 100)
                
                if i % 100 == 0:
                    self.status_message = f"Processing line {i+1}/{self.total}"
                
                try:
                    await self.bucket_mgr.create(
                        content=line,
                        tags=[],
                        importance=5,
                        domain=["imported"],
                        name=f"Imported: {filename[:30]}",
                    )
                except Exception as e:
                    logger.warning(f"Failed to create bucket for line {i}: {e}")
            
            self.status_message = "Import completed successfully"
            self.progress = 100
            
        except Exception as e:
            self.status_message = f"Import failed: {str(e)}"
            logger.error(f"Import failed: {e}")
        finally:
            self.is_running = False
    
    def get_status(self):
        return {
            "running": self.is_running,
            "task": self.current_task,
            "progress": self.progress,
            "total": self.total,
            "message": self.status_message
        }
    
    def pause(self):
        self.is_running = False
        self.status_message = "Import paused"
    
    async def detect_patterns(self):
        return []