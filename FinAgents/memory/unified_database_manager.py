"""
FinAgent Unified Database Manager

This module provides a centralized database management system for the FinAgent Memory Agent.
It handles all Neo4j database operations including connections, memory storage, retrieval,
indexing, and relationship management.

Features:
- Neo4j connection management with health monitoring
- Standardized memory storage and retrieval operations
- Intelligent indexing and semantic search capabilities
- Relationship management for memory graph structures
- Performance monitoring and statistics
- Batch operations for high-throughput scenarios

Author: FinAgent Team
License: Open Source
"""

# ═══════════════════════════════════════════════════════════════════════════════════
# IMPORTS AND DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════════════════════

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Union, Tuple
from dataclasses import dataclass
from enum import Enum

# Neo4j imports
try:
    from neo4j import GraphDatabase, Driver, Session
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False

# Intelligent indexer imports
try:
    from intelligent_memory_indexer import IntelligentMemoryIndexer
    INDEXER_AVAILABLE = True
except ImportError:
    try:
        from .intelligent_memory_indexer import IntelligentMemoryIndexer
        INDEXER_AVAILABLE = True
    except ImportError:
        INDEXER_AVAILABLE = False

# Stream processor imports  
try:
    from realtime_stream_processor import StreamProcessor, ReactiveMemoryManager
    STREAM_PROCESSOR_AVAILABLE = True
except ImportError:
    try:
        from .realtime_stream_processor import StreamProcessor, ReactiveMemoryManager
        STREAM_PROCESSOR_AVAILABLE = True
    except ImportError:
        STREAM_PROCESSOR_AVAILABLE = False

# Configure logging
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════════
# DATA MODELS AND ENUMS
# ═══════════════════════════════════════════════════════════════════════════════════

class MemoryType(Enum):
    """Enumeration of supported memory types"""
    SIGNAL = "signal"
    STRATEGY = "strategy"  
    PERFORMANCE = "performance"
    LEARNING = "learning"
    CONTEXT = "context"
    RELATIONSHIP = "relationship"
    USER_QUERY = "user_query"
    AGENT_RESPONSE = "agent_response"


class RelationshipType(Enum):
    """Enumeration of supported relationship types"""
    CREATED = "CREATED"
    TARGETS = "TARGETS"
    IS_TYPE = "IS_TYPE"
    HAS_PRIORITY = "HAS_PRIORITY"
    HAS_PERFORMANCE = "HAS_PERFORMANCE"
    TIME_SEQUENCE = "TIME_SEQUENCE"
    SIMILAR_SIGNAL = "SIMILAR_SIGNAL"
    RELATES_TO = "RELATES_TO"
    CONTRADICTS = "CONTRADICTS"
    CLARIFIES = "CLARIFIES"
    ENHANCES = "ENHANCES"
    SIMILAR_TO = "SIMILAR_TO"


@dataclass
class DatabaseStats:
    """Data class for database statistics"""
    total_memories: int
    total_agents: int
    total_relationships: int
    memory_types: Dict[str, int]
    agent_activity: Dict[str, int]
    recent_activity: Dict[str, int]
    index_status: Dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════════════
# UNIFIED DATABASE MANAGER CLASS
# ═══════════════════════════════════════════════════════════════════════════════════

class UnifiedDatabaseManager:
    """
    Centralized database manager for FinAgent memory operations.
    
    This class provides a high-level interface for all database operations,
    ensuring consistency, performance, and reliability across the system.
    It integrates with the original TradingGraphMemory for backwards compatibility.
    """
    
    def __init__(self, 
                 uri: str = "bolt://localhost:7687",
                 username: str = "neo4j", 
                 password: str = "FinOrchestration",
                 database: str = "neo4j",
                 max_connection_lifetime: int = 3600,
                 max_connection_pool_size: int = 50):
        """
        Initialize the unified database manager.
        
        Args:
            uri: Neo4j connection URI
            username: Database username  
            password: Database password
            database: Target database name
            max_connection_lifetime: Maximum connection lifetime in seconds
            max_connection_pool_size: Maximum connection pool size
        """
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database
        self.driver: Optional[Driver] = None
        self.is_connected = False
        
        # Connection configuration
        self.max_connection_lifetime = max_connection_lifetime
        self.max_connection_pool_size = max_connection_pool_size
        
        # Initialize intelligent indexer if available
        self.indexer: Optional[IntelligentMemoryIndexer] = None
        if INDEXER_AVAILABLE:
            try:
                self.indexer = IntelligentMemoryIndexer()
                logger.info("✅ Intelligent memory indexer initialized")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize intelligent indexer: {e}")
        
        # Initialize stream processor if available
        self.stream_processor: Optional[StreamProcessor] = None
        self.reactive_manager: Optional[ReactiveMemoryManager] = None
        if STREAM_PROCESSOR_AVAILABLE:
            try:
                self.stream_processor = StreamProcessor()
                self.reactive_manager = ReactiveMemoryManager(self.stream_processor)
                logger.info("✅ Real-time stream processor initialized")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize stream processor: {e}")
        
        # Performance metrics
        self.operation_count = 0
        self.last_health_check = None
        
        logger.info(f"Unified Database Manager initialized for: {database}")

    # ═══════════════════════════════════════════════════════════════════════════════════
    # CONNECTION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════════════

    async def connect(self) -> bool:
        """
        Establish connection to Neo4j database with proper configuration.
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            if not NEO4J_AVAILABLE:
                logger.error("❌ Neo4j driver not available. Install with: pip install neo4j")
                return False
            
            # Create driver with optimized settings
            self.driver = GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
                max_connection_lifetime=self.max_connection_lifetime,
                max_connection_pool_size=self.max_connection_pool_size,
                connection_acquisition_timeout=60
            )
            
            # Test connection
            with self.driver.session(database=self.database) as session:
                result = session.run("RETURN 1 as test_value")
                test_record = result.single()
                if test_record["test_value"] != 1:
                    raise Exception("Connection test failed")
            
            self.is_connected = True
            self.last_health_check = datetime.utcnow()
            
            # Initialize database schema
            await self._initialize_schema()
            
            logger.info(f"✅ Connected to Neo4j database: {self.database} at {self.uri}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to connect to Neo4j: {e}")
            self.is_connected = False
            return False

    async def close(self) -> None:
        """Close the database connection gracefully."""
        try:
            if self.driver:
                self.driver.close()
                self.is_connected = False
                logger.info("✅ Neo4j connection closed")
            
            # Close stream processor if available
            if self.stream_processor and hasattr(self.stream_processor, "stop_processing"):
                await self.stream_processor.stop_processing()
                
        except Exception as e:
            logger.error(f"❌ Error closing connections: {e}")

    # ═══════════════════════════════════════════════════════════════════════════════════
    # MEMORY STORAGE OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════════════════

    async def store_memory(self, 
                          query: str, 
                          keywords: List[str], 
                          summary: str, 
                          agent_id: str,
                          event_type: str = 'USER_QUERY',
                          log_level: str = 'INFO',
                          session_id: Optional[str] = None,
                          correlation_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Store a memory record with proper validation and indexing.
        Compatible with original TradingGraphMemory interface.
        
        Args:
            query: The original query or content
            keywords: List of keywords associated with the memory
            summary: Summary of the memory content
            agent_id: ID of the agent creating the memory
            event_type: Type of event (USER_QUERY, AGENT_RESPONSE, etc.)
            log_level: Logging level
            session_id: Optional session identifier
            correlation_id: Optional correlation identifier
            
        Returns:
            Dict[str, Any]: Storage result with memory details
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        try:
            # Generate memory ID and prepare data
            memory_id = str(uuid.uuid4())
            timestamp = datetime.utcnow()
            
            # Create memory content structure
            memory_content = {
                "query": query,
                "summary": summary,
                "keywords": keywords,
                "event_type": event_type,
                "log_level": log_level,
                "session_id": session_id,
                "correlation_id": correlation_id
            }
            
            # Store memory node
            with self.driver.session(database=self.database) as session:
                memory_query = """
                CREATE (m:Memory {
                    memory_id: $memory_id,
                    agent_id: $agent_id,
                    memory_type: $memory_type,
                    content: $content,
                    content_text: $content_text,
                    summary: $summary,
                    keywords: $keywords,
                    timestamp: datetime($timestamp),
                    event_type: $event_type,
                    log_level: $log_level,
                    session_id: $session_id,
                    correlation_id: $correlation_id,
                    created_at: datetime(),
                    lookup_count: 0,
                    embedding: $embedding,
                    embedding_model: $embedding_model,
                    embedding_dim: $embedding_dim,
                    needs_embedding: $needs_embedding
                })
                RETURN m.memory_id as stored_id
                """
                
                # Prepare searchable text
                content_text = f"{query} {summary} {' '.join(keywords)}".lower()

                # Embed on write (bge-m3 via the indexer). On failure, store the node
                # anyway and flag needs_embedding=true for a later backfill pass.
                embedding = None
                embedding_model = getattr(self.indexer, "embedding_model_name", None) if self.indexer else None
                embedding_dim = getattr(self.indexer, "embedding_dim", 0) if self.indexer else 0
                needs_embedding = True
                if self.indexer:
                    try:
                        vec = self.indexer.create_text_embedding(content_text)
                        embedding = [float(x) for x in vec]
                        needs_embedding = False
                    except Exception as e:
                        logger.warning(f"⚠️ Embed-on-write failed (stored without vector): {e}")
                
                result = session.run(memory_query, {
                    "memory_id": memory_id,
                    "agent_id": agent_id,
                    "memory_type": event_type.lower(),
                    "content": json.dumps(memory_content),
                    "content_text": content_text,
                    "summary": summary,
                    "keywords": keywords,
                    "timestamp": timestamp.isoformat(),
                    "event_type": event_type,
                    "log_level": log_level,
                    "session_id": session_id,
                    "correlation_id": correlation_id,
                    "embedding": embedding,
                    "embedding_model": embedding_model,
                    "embedding_dim": embedding_dim,
                    "needs_embedding": needs_embedding
                })
                
                stored_id = result.single()["stored_id"]
                
                # Create agent relationship
                await self._ensure_agent_node(session, agent_id)
                
                # Find and link similar memories
                linked_memories = await self._find_and_link_similar_memories(session, memory_id, keywords, summary)
                
                # Embeddings are persisted on the node (embed-on-write) and searched via the
                # Neo4j vector index — the in-process pickle index is retired as the search path.
                
                # Publish memory event to stream processor
                if self.reactive_manager and hasattr(self.reactive_manager, "handle_memory_event"):
                    await self.reactive_manager.handle_memory_event({
                        "event_type": "memory_stored",
                        "memory_id": memory_id,
                        "agent_id": agent_id,
                        "timestamp": timestamp.isoformat()
                    })
                
                self.operation_count += 1
                
                return {
                    "memory_id": stored_id,
                    "agent_id": agent_id,
                    "timestamp": timestamp.isoformat(),
                    "content": memory_content,
                    "linked_memories": linked_memories,
                    "status": "success"
                }
                
        except Exception as e:
            logger.error(f"❌ Failed to store memory: {e}")
            raise Exception(f"Memory storage failed: {str(e)}")

    async def vector_search(self, query_embedding, limit: int = 10,
                            similarity_threshold: float = 0.0) -> List[Dict[str, Any]]:
        """Semantic search via the Neo4j native vector index (cosine, 1024-dim)."""
        if not self.is_connected:
            raise Exception("Database not connected")
        vec = [float(x) for x in query_embedding]
        cypher = """
        CALL db.index.vector.queryNodes('memory_embedding_index', $k, $vec)
        YIELD node, score
        WHERE score >= $threshold
        RETURN node.memory_id AS memory_id, node.agent_id AS agent_id,
               node.summary AS summary, node.content AS content,
               node.keywords AS keywords, node.event_type AS event_type,
               node.timestamp AS timestamp, score AS similarity_score
        ORDER BY score DESC
        """
        results: List[Dict[str, Any]] = []
        with self.driver.session(database=self.database) as session:
            for r in session.run(cypher, {"k": int(limit), "vec": vec,
                                          "threshold": float(similarity_threshold)}):
                results.append({
                    "memory_id": r["memory_id"],
                    "agent_id": r["agent_id"],
                    "summary": r["summary"],
                    "content": json.loads(r["content"]) if r.get("content") else {},
                    "keywords": r["keywords"],
                    "event_type": r["event_type"],
                    "timestamp": r["timestamp"].isoformat() if r.get("timestamp") else None,
                    "similarity_score": float(r["similarity_score"]),
                })
        self.operation_count += 1
        return results

    # ═══════════════════════════════════════════════════════════════════════════════════
    # GBRAIN GENERALIZATION — domain-agnostic :Page nodes (Phase 2)
    # ═══════════════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _slugify(title: str) -> str:
        s = "".join(c if c.isalnum() else "-" for c in (title or "").lower())
        s = "-".join(filter(None, s.split("-")))[:80]
        return s or str(uuid.uuid4())

    async def put_page(self, title: str, body: str, namespace: str = "default",
                       slug: Optional[str] = None, tags: Optional[List[str]] = None,
                       kind: str = "knowledge", links: Optional[List[str]] = None,
                       written_by: Optional[str] = None, agent_type: Optional[str] = None,
                       agent_instance_id: Optional[str] = None, source: Optional[str] = None,
                       trust: str = "trusted") -> Dict[str, Any]:
        """Upsert a knowledge page (gbrain put_page). Embeds title+body; bumps version on update."""
        if not self.is_connected:
            raise Exception("Database not connected")
        tags = tags or []
        links = links or []
        slug = slug or self._slugify(title)
        page_key = f"{namespace}::{slug}"

        text = f"{title}\n\n{body}".strip()
        embedding = None
        needs_embedding = True
        embedding_model = getattr(self.indexer, "embedding_model_name", None) if self.indexer else None
        embedding_dim = getattr(self.indexer, "embedding_dim", 0) if self.indexer else 0
        if self.indexer:
            try:
                v = self.indexer.create_text_embedding(text)
                embedding = [float(x) for x in v]
                needs_embedding = False
            except Exception as e:
                logger.warning(f"⚠️ Page embed-on-write failed (stored without vector): {e}")

        cypher = """
        MERGE (p:Page {page_key: $page_key})
        ON CREATE SET p.id = $id, p.created_at = datetime(), p.version = 1
        ON MATCH  SET p.version = coalesce(p.version, 0) + 1
        SET p.namespace = $namespace, p.slug = $slug, p.title = $title, p.body = $body,
            p.tags = $tags, p.kind = $kind, p.embedding = $embedding,
            p.embedding_model = $embedding_model, p.embedding_dim = $embedding_dim,
            p.needs_embedding = $needs_embedding, p.written_by = $written_by,
            p.agent_type = $agent_type, p.agent_instance_id = $agent_instance_id,
            p.source = $source, p.trust = $trust, p.updated_at = datetime()
        RETURN p.id AS id, p.version AS version, p.namespace AS namespace, p.slug AS slug
        """
        with self.driver.session(database=self.database) as session:
            rec = session.run(cypher, {
                "page_key": page_key, "id": str(uuid.uuid4()), "namespace": namespace,
                "slug": slug, "title": title, "body": body, "tags": tags, "kind": kind,
                "embedding": embedding, "embedding_model": embedding_model,
                "embedding_dim": embedding_dim, "needs_embedding": needs_embedding,
                "written_by": written_by, "agent_type": agent_type,
                "agent_instance_id": agent_instance_id, "source": source, "trust": trust,
            }).single()
            for tgt in links:
                session.run(
                    "MATCH (a:Page {page_key:$a}), (b:Page {page_key:$b}) MERGE (a)-[:LINKS_TO]->(b)",
                    {"a": page_key, "b": f"{namespace}::{tgt}"})
        self.operation_count += 1
        return {"id": rec["id"], "namespace": rec["namespace"], "slug": rec["slug"],
                "version": rec["version"], "needs_embedding": needs_embedding, "status": "success"}

    async def get_page(self, slug: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """Fetch a page by (namespace, slug) with its outgoing links."""
        if not self.is_connected:
            raise Exception("Database not connected")
        cypher = """
        MATCH (p:Page {page_key: $k})
        OPTIONAL MATCH (p)-[:LINKS_TO]->(t:Page)
        RETURN p AS page, collect(t.slug) AS links
        """
        with self.driver.session(database=self.database) as session:
            rec = session.run(cypher, {"k": f"{namespace}::{slug}"}).single()
        if not rec:
            return None
        p = rec["page"]
        return {
            "id": p.get("id"), "namespace": p.get("namespace"), "slug": p.get("slug"),
            "title": p.get("title"), "body": p.get("body"), "tags": p.get("tags"),
            "kind": p.get("kind"), "version": p.get("version"),
            "source": p.get("source"), "trust": p.get("trust"),
            "links": [l for l in rec["links"] if l],
            "updated_at": p.get("updated_at").isoformat() if p.get("updated_at") else None,
        }

    async def search_pages(self, query_embedding, namespace: str = "default", limit: int = 10,
                          kind: Optional[str] = None, similarity_threshold: float = 0.0) -> List[Dict[str, Any]]:
        """Semantic search over :Page via the Neo4j vector index, scoped to a namespace (and optional kind)."""
        if not self.is_connected:
            raise Exception("Database not connected")
        vec = [float(x) for x in query_embedding]
        fetch = max(int(limit) * 5, 20)  # over-fetch, then filter by namespace/kind
        cypher = """
        CALL db.index.vector.queryNodes('page_embedding_index', $fetch, $vec)
        YIELD node, score
        WHERE node.namespace = $ns AND ($kind IS NULL OR node.kind = $kind) AND score >= $th
        RETURN node.slug AS slug, node.title AS title, node.kind AS kind,
               node.tags AS tags, node.namespace AS namespace, score AS similarity_score
        ORDER BY score DESC
        LIMIT $limit
        """
        results: List[Dict[str, Any]] = []
        with self.driver.session(database=self.database) as session:
            for r in session.run(cypher, {"fetch": fetch, "vec": vec, "ns": namespace,
                                          "kind": kind, "th": float(similarity_threshold),
                                          "limit": int(limit)}):
                results.append({"slug": r["slug"], "title": r["title"], "kind": r["kind"],
                                "tags": r["tags"], "namespace": r["namespace"],
                                "similarity_score": float(r["similarity_score"])})
        return results

    async def link_pages(self, from_slug: str, to_slug: str, namespace: str = "default") -> Dict[str, Any]:
        """Create a (:Page)-[:LINKS_TO]->(:Page) edge within a namespace."""
        if not self.is_connected:
            raise Exception("Database not connected")
        cypher = """
        MATCH (a:Page {page_key:$a}), (b:Page {page_key:$b})
        MERGE (a)-[:LINKS_TO]->(b)
        RETURN count(*) AS linked
        """
        with self.driver.session(database=self.database) as session:
            rec = session.run(cypher, {"a": f"{namespace}::{from_slug}",
                                       "b": f"{namespace}::{to_slug}"}).single()
        return {"linked": rec["linked"] if rec else 0, "from": from_slug,
                "to": to_slug, "namespace": namespace}

    async def store_memories_batch(self, events: List[Dict[str, Any]]) -> int:
        """
        Store multiple memories in a batch operation for high throughput.
        
        Args:
            events: List of memory events to store
            
        Returns:
            int: Number of memories successfully stored
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        stored_count = 0
        
        try:
            for event in events:
                # Ensure required fields exist
                if not all(key in event for key in ["agent_id", "content"]):
                    continue
                
                # Extract fields with defaults
                query = event.get("query", str(event.get("content", "")))
                keywords = event.get("keywords", [])
                summary = event.get("summary", query[:100] + "...")
                agent_id = event["agent_id"]
                event_type = event.get("event_type", "BATCH_EVENT")
                
                await self.store_memory(
                    query=query,
                    keywords=keywords,
                    summary=summary,
                    agent_id=agent_id,
                    event_type=event_type,
                    session_id=event.get("session_id"),
                    correlation_id=event.get("correlation_id")
                )
                
                stored_count += 1
                
        except Exception as e:
            logger.error(f"❌ Batch storage error: {e}")
            
        return stored_count

    # ═══════════════════════════════════════════════════════════════════════════════════
    # MEMORY RETRIEVAL OPERATIONS  
    # ═══════════════════════════════════════════════════════════════════════════════════

    async def retrieve_memory(self, search_query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Retrieve memories using full-text search.
        Compatible with original TradingGraphMemory interface.
        
        Args:
            search_query: Text to search for
            limit: Maximum number of results
            
        Returns:
            List[Dict[str, Any]]: List of matching memories
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        try:
            with self.driver.session(database=self.database) as session:
                # Use intelligent search if available
                if self.indexer and search_query:
                    return await self._semantic_search(search_query, limit)
                
                # Fallback to text search
                search_query_lower = search_query.lower()
                query = """
                MATCH (m:Memory)
                WHERE m.content_text CONTAINS $search_text
                   OR m.summary CONTAINS $search_text
                   OR ANY(keyword IN m.keywords WHERE keyword CONTAINS $search_text)
                SET m.lookup_count = m.lookup_count + 1
                RETURN m.memory_id, m.agent_id, m.memory_type, m.content, m.summary,
                       m.keywords, m.timestamp, m.event_type, m.lookup_count
                ORDER BY m.timestamp DESC
                LIMIT $limit
                """
                
                result = session.run(query, {
                    "search_text": search_query_lower,
                    "limit": limit
                })
                
                memories = []
                for record in result:
                    try:
                        # Safely extract all required fields from record
                        memory = {
                            "memory_id": record.get("memory_id", ""),
                            "agent_id": record.get("agent_id", ""),
                            "memory_type": record.get("memory_type", ""),
                            "content": json.loads(record["content"]) if record.get("content") else {},
                            "summary": record.get("summary", ""),
                            "keywords": record.get("keywords", []),
                            "timestamp": record["timestamp"].isoformat() if record.get("timestamp") else None,
                            "event_type": record.get("event_type", ""),
                            "lookup_count": record.get("lookup_count", 0)
                        }
                        
                        # Only add memory if it has essential fields
                        if memory["memory_id"]:
                            memories.append(memory)
                        else:
                            logger.debug("⚠️ Skipping memory record with missing memory_id")
                            
                    except Exception as record_error:
                        logger.debug(f"⚠️ Error processing memory record: {record_error}")
                        continue
                
                return memories
                
        except Exception as e:
            logger.error(f"❌ Memory retrieval failed: {e}")
            return []

    async def retrieve_memory_with_expansion(self, search_query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Retrieve memories with relationship expansion.
        
        Args:
            search_query: Text to search for
            limit: Maximum number of results
            
        Returns:
            List[Dict[str, Any]]: List of memories with related memories
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        try:
            # Get initial search results
            initial_results = await self.retrieve_memory(search_query, limit // 2)
            
            if not initial_results:
                return []
            
            # Expand with related memories
            with self.driver.session(database=self.database) as session:
                expanded_memories = []
                
                for memory in initial_results:
                    # Add original memory
                    expanded_memories.append(memory)
                    
                    # Find related memories
                    expansion_query = """
                    MATCH (m:Memory {memory_id: $memory_id})-[:SIMILAR_TO|:RELATES_TO]-(related:Memory)
                    RETURN related.memory_id, related.agent_id, related.memory_type, 
                           related.content, related.summary, related.keywords, 
                           related.timestamp, related.event_type
                    LIMIT 3
                    """
                    
                    result = session.run(expansion_query, memory_id=memory["memory_id"])
                    
                    for related_record in result:
                        related_memory = {
                            "memory_id": related_record["memory_id"],
                            "agent_id": related_record["agent_id"],
                            "memory_type": related_record["memory_type"],
                            "content": json.loads(related_record["content"]) if related_record["content"] else {},
                            "summary": related_record["summary"],
                            "keywords": related_record["keywords"],
                            "timestamp": related_record["timestamp"].isoformat() if related_record["timestamp"] else None,
                            "event_type": related_record["event_type"],
                            "is_related": True,
                            "related_to": memory["memory_id"]
                        }
                        expanded_memories.append(related_memory)
                
                return expanded_memories[:limit]
                
        except Exception as e:
            logger.error(f"❌ Expanded memory retrieval failed: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════════════════════════
    # FILTERING AND ANALYTICS
    # ═══════════════════════════════════════════════════════════════════════════════════

    async def filter_memories(self, filters: Dict[str, Any], limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        Filter memories based on structured criteria.
        
        Args:
            filters: Dictionary of filter criteria
            limit: Maximum number of results
            offset: Number of results to skip
            
        Returns:
            List[Dict[str, Any]]: Filtered memories
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        try:
            with self.driver.session(database=self.database) as session:
                # Build dynamic query
                where_clauses = []
                params = {"limit": limit, "offset": offset}
                
                if "agent_id" in filters:
                    where_clauses.append("m.agent_id = $agent_id")
                    params["agent_id"] = filters["agent_id"]
                
                if "event_type" in filters:
                    where_clauses.append("m.event_type = $event_type")
                    params["event_type"] = filters["event_type"]
                
                if "session_id" in filters:
                    where_clauses.append("m.session_id = $session_id")
                    params["session_id"] = filters["session_id"]
                
                if "start_time" in filters:
                    where_clauses.append("m.timestamp >= datetime($start_time)")
                    params["start_time"] = filters["start_time"]
                
                if "end_time" in filters:
                    where_clauses.append("m.timestamp <= datetime($end_time)")
                    params["end_time"] = filters["end_time"]
                
                # Construct query
                base_query = "MATCH (m:Memory)"
                if where_clauses:
                    base_query += " WHERE " + " AND ".join(where_clauses)
                
                query = f"""
                {base_query}
                RETURN m.memory_id, m.agent_id, m.memory_type, m.content, m.summary,
                       m.keywords, m.timestamp, m.event_type, m.lookup_count
                ORDER BY m.timestamp DESC
                SKIP $offset LIMIT $limit
                """
                
                result = session.run(query, params)
                
                memories = []
                for record in result:
                    memory = {
                        "memory_id": record["memory_id"],
                        "agent_id": record["agent_id"],
                        "memory_type": record["memory_type"],
                        "content": json.loads(record["content"]) if record["content"] else {},
                        "summary": record["summary"],
                        "keywords": record["keywords"],
                        "timestamp": record["timestamp"].isoformat() if record["timestamp"] else None,
                        "event_type": record["event_type"],
                        "lookup_count": record["lookup_count"]
                    }
                    memories.append(memory)
                
                return memories
                
        except Exception as e:
            logger.error(f"❌ Memory filtering failed: {e}")
            return []

    async def get_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive database statistics.
        Compatible with original TradingGraphMemory interface.
        
        Returns:
            Dict[str, Any]: Database statistics
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        try:
            with self.driver.session(database=self.database) as session:
                # Get basic counts
                stats_query = """
                MATCH (m:Memory)
                WITH count(m) as memory_count
                MATCH (a:Agent)
                WITH memory_count, count(a) as agent_count
                MATCH ()-[r]->()
                RETURN memory_count, agent_count, count(r) as relationship_count
                """
                
                result = session.run(stats_query)
                basic_stats_record = result.single()
                
                # Safely extract basic statistics
                basic_stats = {
                    "memory_count": basic_stats_record.get("memory_count", 0) if basic_stats_record else 0,
                    "agent_count": basic_stats_record.get("agent_count", 0) if basic_stats_record else 0,
                    "relationship_count": basic_stats_record.get("relationship_count", 0) if basic_stats_record else 0
                }
                
                # Get memory type distribution
                type_query = """
                MATCH (m:Memory)
                RETURN m.memory_type as type, count(m) as count
                """
                
                result = session.run(type_query)
                memory_types = {}
                for record in result:
                    if record.get("type") and record.get("count") is not None:
                        memory_types[record["type"]] = record["count"]
                
                # Get agent activity (using agent_id property instead of CREATED relationship)
                activity_query = """
                MATCH (m:Memory)
                WHERE m.agent_id IS NOT NULL
                WITH m.agent_id as agent, count(m) as activity
                ORDER BY activity DESC
                LIMIT 10
                RETURN agent, activity
                """
                
                result = session.run(activity_query)
                agent_activity = {}
                for record in result:
                    if record.get("agent") and record.get("activity") is not None:
                        agent_activity[record["agent"]] = record["activity"]
                
                return {
                    "total_memories": basic_stats["memory_count"] or 0,
                    "total_agents": basic_stats["agent_count"] or 0,
                    "total_relationships": basic_stats["relationship_count"] or 0,
                    "memory_types": memory_types,
                    "agent_activity": agent_activity,
                    "operation_count": self.operation_count,
                    "indexer_available": self.indexer is not None,
                    "stream_processor_available": self.stream_processor is not None
                }
                
        except Exception as e:
            logger.error(f"❌ Statistics retrieval failed: {e}")
            return {}

    # ═══════════════════════════════════════════════════════════════════════════════════
    # RELATIONSHIP MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════════════

    async def create_relationship(self, 
                                source_memory_id: str, 
                                target_memory_id: str, 
                                relationship_type: str) -> Optional[str]:
        """
        Create a relationship between two memory nodes.
        
        Args:
            source_memory_id: Source memory ID
            target_memory_id: Target memory ID
            relationship_type: Type of relationship
            
        Returns:
            Optional[str]: Relationship type if successful, None otherwise
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        try:
            with self.driver.session(database=self.database) as session:
                # Validate relationship type
                valid_types = [rt.value for rt in RelationshipType]
                if relationship_type not in valid_types:
                    relationship_type = "RELATES_TO"
                
                query = f"""
                MATCH (source:Memory {{memory_id: $source_id}})
                MATCH (target:Memory {{memory_id: $target_id}})
                CREATE (source)-[r:{relationship_type}]->(target)
                SET r.created_at = datetime()
                RETURN type(r) as relationship_type
                """
                
                result = session.run(query, {
                    "source_id": source_memory_id,
                    "target_id": target_memory_id
                })
                
                record = result.single()
                if record:
                    return record["relationship_type"]
                else:
                    return None
                    
        except Exception as e:
            logger.error(f"❌ Relationship creation failed: {e}")
            return None

    # ═══════════════════════════════════════════════════════════════════════════════════
    # MAINTENANCE OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════════════════

    async def prune_memories(self, max_age_days: int = 180, min_lookup_count: int = 1) -> int:
        """
        Delete old and irrelevant memories.
        
        Args:
            max_age_days: Maximum age in days
            min_lookup_count: Minimum lookup count to keep
            
        Returns:
            int: Number of deleted memories
        """
        if not self.is_connected:
            raise Exception("Database not connected")
        
        try:
            with self.driver.session(database=self.database) as session:
                cutoff_date = datetime.utcnow() - timedelta(days=max_age_days)
                
                delete_query = """
                MATCH (m:Memory)
                WHERE m.timestamp < datetime($cutoff_date)
                  AND m.lookup_count < $min_lookup_count
                WITH m, count(m) as to_delete
                DETACH DELETE m
                RETURN to_delete
                """
                
                result = session.run(delete_query, {
                    "cutoff_date": cutoff_date.isoformat(),
                    "min_lookup_count": min_lookup_count
                })
                
                deleted_count = result.single()
                return deleted_count["to_delete"] if deleted_count else 0
                
        except Exception as e:
            logger.error(f"❌ Memory pruning failed: {e}")
            return 0

    async def create_memory_index(self) -> None:
        """Create full-text search index for memories."""
        if not self.is_connected:
            return
        
        try:
            with self.driver.session(database=self.database) as session:
                # Create text index for content search
                index_query = """
                CREATE FULLTEXT INDEX memory_content_index IF NOT EXISTS
                FOR (m:Memory) ON EACH [m.content_text, m.summary]
                """
                session.run(index_query)
                logger.info("✅ Memory content index created")
                
        except Exception as e:
            logger.debug(f"Index creation note: {e}")

    async def create_structured_indexes(self) -> None:
        """Create structured property indexes."""
        if not self.is_connected:
            return
        
        try:
            with self.driver.session(database=self.database) as session:
                indexes = [
                    "CREATE INDEX memory_timestamp_idx IF NOT EXISTS FOR (m:Memory) ON (m.timestamp)",
                    "CREATE INDEX memory_agent_idx IF NOT EXISTS FOR (m:Memory) ON (m.agent_id)",
                    "CREATE INDEX memory_type_idx IF NOT EXISTS FOR (m:Memory) ON (m.memory_type)",
                    "CREATE INDEX agent_id_idx IF NOT EXISTS FOR (a:Agent) ON (a.agent_id)"
                ]
                
                for index_query in indexes:
                    session.run(index_query)
                
                logger.info("✅ Structured indexes created")
                
        except Exception as e:
            logger.debug(f"Structured index creation note: {e}")

    # ═══════════════════════════════════════════════════════════════════════════════════
    # PRIVATE HELPER METHODS
    # ═══════════════════════════════════════════════════════════════════════════════════

    async def _initialize_schema(self) -> None:
        """Initialize database schema with constraints and indexes."""
        if not self.is_connected:
            return
        
        try:
            with self.driver.session(database=self.database) as session:
                # Create constraints
                constraints = [
                    "CREATE CONSTRAINT memory_id_unique IF NOT EXISTS FOR (m:Memory) REQUIRE m.memory_id IS UNIQUE",
                    "CREATE CONSTRAINT agent_id_unique IF NOT EXISTS FOR (a:Agent) REQUIRE a.agent_id IS UNIQUE"
                ]
                
                for constraint in constraints:
                    try:
                        session.run(constraint)
                    except Exception as e:
                        logger.debug(f"Constraint note: {e}")

                # Vector index for semantic search (Neo4j 5.x native vector index).
                # Dimension/similarity are LOCKED to the embedding model: bge-m3 = 1024, cosine.
                # Switching to any other 1024-dim model (gte-large-en-v1.5, e5-large-v2,
                # qwen3-embedding-0.6b) requires only re-embedding, not an index rebuild.
                try:
                    session.run(
                        """
                        CREATE VECTOR INDEX memory_embedding_index IF NOT EXISTS
                        FOR (m:Memory) ON (m.embedding)
                        OPTIONS { indexConfig: {
                            `vector.dimensions`: 1024,
                            `vector.similarity_function`: 'cosine'
                        }}
                        """
                    )
                    logger.info("✅ Vector index (1024-dim, cosine) ensured on :Memory(embedding)")
                except Exception as e:
                    logger.warning(f"Vector index note (requires Neo4j 5.x): {e}")

                # :Page (gbrain generalization) — synthetic composite key (namespace::slug) for
                # portability across Neo4j editions, plus its own 1024/cosine vector index.
                try:
                    session.run("CREATE CONSTRAINT page_key_unique IF NOT EXISTS FOR (p:Page) REQUIRE p.page_key IS UNIQUE")
                    session.run(
                        """
                        CREATE VECTOR INDEX page_embedding_index IF NOT EXISTS
                        FOR (p:Page) ON (p.embedding)
                        OPTIONS { indexConfig: {
                            `vector.dimensions`: 1024,
                            `vector.similarity_function`: 'cosine'
                        }}
                        """
                    )
                    logger.info("✅ :Page constraint + vector index (1024, cosine) ensured")
                except Exception as e:
                    logger.warning(f"Page schema note: {e}")

                # Create indexes
                await self.create_memory_index()
                await self.create_structured_indexes()
                
        except Exception as e:
            logger.warning(f"Schema initialization warning: {e}")

    async def _ensure_agent_node(self, session, agent_id: str) -> None:
        """Ensure agent node exists."""
        agent_query = """
        MERGE (a:Agent {agent_id: $agent_id})
        ON CREATE SET 
            a.created_at = datetime(),
            a.memory_count = 1,
            a.last_active = datetime()
        ON MATCH SET 
            a.memory_count = a.memory_count + 1,
            a.last_active = datetime()
        """
        
        session.run(agent_query, agent_id=agent_id)

    async def _find_and_link_similar_memories(self, session, memory_id: str, keywords: List[str], summary: str) -> List[str]:
        """Find and link similar memories."""
        if not keywords:
            return []
        
        try:
            # Find memories with overlapping keywords
            similar_query = """
            MATCH (m:Memory)
            WHERE m.memory_id <> $memory_id
              AND ANY(keyword IN m.keywords WHERE keyword IN $keywords)
            RETURN m.memory_id
            LIMIT 5
            """
            
            result = session.run(similar_query, {
                "memory_id": memory_id,
                "keywords": keywords
            })
            
            similar_memories = []
            for record in result:
                try:
                    # Safely extract memory_id from record
                    if "memory_id" in record and record["memory_id"]:
                        similar_id = record["memory_id"]
                        
                        # Create SIMILAR_TO relationship
                        link_query = """
                        MATCH (m1:Memory {memory_id: $memory_id})
                        MATCH (m2:Memory {memory_id: $similar_id})
                        CREATE (m1)-[:SIMILAR_TO {created_at: datetime(), similarity_type: 'keyword'}]->(m2)
                        """
                        
                        session.run(link_query, {
                            "memory_id": memory_id,
                            "similar_id": similar_id
                        })
                        
                        similar_memories.append(similar_id)
                    else:
                        logger.debug("⚠️ Record missing memory_id field")
                except Exception as record_error:
                    logger.debug(f"⚠️ Error processing similar memory record: {record_error}")
                    continue
            
            return similar_memories
            
        except Exception as e:
            logger.warning(f"⚠️ Similar memory linking failed: {e}")
            return []

    async def _semantic_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Perform semantic search using intelligent indexer."""
        if not self.indexer:
            return []
        
        try:
            # Get all memories for semantic search
            with self.driver.session(database=self.database) as session:
                all_memories_query = """
                MATCH (m:Memory)
                RETURN m.memory_id, m.content, m.summary, m.keywords
                LIMIT 1000
                """
                
                result = session.run(all_memories_query)
                all_memories = []
                
                for record in result:
                    memory_data = {
                        "memory_id": record["memory_id"],
                        "content": record["content"],
                        "summary": record["summary"],
                        "keywords": record["keywords"]
                    }
                    all_memories.append(memory_data)
                
                # Perform semantic search
                search_results = self.indexer.semantic_search(
                    query=query,
                    memories=all_memories,
                    top_k=limit
                )
                
                # Get full memory details
                memories = []
                for memory_id, similarity_score in search_results:
                    memory_query = """
                    MATCH (m:Memory {memory_id: $memory_id})
                    SET m.lookup_count = m.lookup_count + 1
                    RETURN m.memory_id, m.agent_id, m.memory_type, m.content, m.summary,
                           m.keywords, m.timestamp, m.event_type, m.lookup_count
                    """
                    
                    memory_result = session.run(memory_query, memory_id=memory_id)
                    record = memory_result.single()
                    
                    if record:
                        memory = {
                            "memory_id": record["memory_id"],
                            "agent_id": record["agent_id"],
                            "memory_type": record["memory_type"],
                            "content": json.loads(record["content"]) if record["content"] else {},
                            "summary": record["summary"],
                            "keywords": record["keywords"],
                            "timestamp": record["timestamp"].isoformat() if record["timestamp"] else None,
                            "event_type": record["event_type"],
                            "lookup_count": record["lookup_count"],
                            "similarity_score": float(similarity_score)
                        }
                        memories.append(memory)
                
                return memories
                
        except Exception as e:
            logger.error(f"❌ Semantic search failed: {e}")
            return []


# ═══════════════════════════════════════════════════════════════════════════════════
# FACTORY FUNCTIONS AND COMPATIBILITY LAYER
# ═══════════════════════════════════════════════════════════════════════════════════

class TradingGraphMemory(UnifiedDatabaseManager):
    """
    Compatibility layer for the original TradingGraphMemory class.
    Provides the same interface while using the new unified manager.
    """
    
    def __init__(self, uri: str, username: str, password: str):
        """Initialize with original interface."""
        super().__init__(uri=uri, username=username, password=password)
        
        # Start connection automatically for compatibility
        self.driver = None
        self._connect_sync()
    
    def _connect_sync(self):
        """Synchronous connection for compatibility."""
        try:
            if NEO4J_AVAILABLE:
                self.driver = GraphDatabase.driver(
                    self.uri,
                    auth=(self.username, self.password)
                )
                self.is_connected = True
        except Exception as e:
            logger.error(f"❌ Sync connection failed: {e}")


def create_database_manager(config: Optional[Dict[str, Any]] = None) -> UnifiedDatabaseManager:
    """
    Factory function to create a configured database manager.
    
    Args:
        config: Optional configuration dictionary
        
    Returns:
        UnifiedDatabaseManager: Configured database manager instance
    """
    config = config or {}
    
    return UnifiedDatabaseManager(
        uri=config.get("uri", "bolt://localhost:7687"),
        username=config.get("username", "neo4j"),
        password=config.get("password", "finagent123"),
        database=config.get("database", "neo4j"),
        max_connection_lifetime=config.get("max_connection_lifetime", 3600),
        max_connection_pool_size=config.get("max_connection_pool_size", 50)
    )


# ═══════════════════════════════════════════════════════════════════════════════════
# TESTING AND VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    
    async def test_unified_database():
        """Test the unified database manager functionality."""
        print("🧪 Testing Unified Database Manager")
        
        db = create_database_manager()
        
        if await db.connect():
            print("✅ Database connected successfully")
            
            # Test memory storage
            test_memory = await db.store_memory(
                query="Test memory storage",
                keywords=["test", "storage"],
                summary="Testing the unified database manager",
                agent_id="test_agent",
                event_type="TEST_EVENT"
            )
            print(f"✅ Stored test memory: {test_memory['memory_id']}")
            
            # Test memory retrieval
            memories = await db.retrieve_memory("test", limit=5)
            print(f"✅ Retrieved {len(memories)} memories")
            
            # Test statistics
            stats = await db.get_statistics()
            print(f"✅ Database stats: {stats.get('total_memories', 0)} memories")
            
            await db.close()
        else:
            print("❌ Database connection failed")
    
    asyncio.run(test_unified_database())
