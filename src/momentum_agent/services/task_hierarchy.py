"""
任务层级服务模块 - Task Hierarchy Service Module
提供子任务管理和任务关联功能
"""
from typing import TYPE_CHECKING, Optional
from datetime import datetime

if TYPE_CHECKING:
    from ..models import Task, TaskRelation, TaskRelationType, Priority


class TaskHierarchyService:
    """任务层级服务类 - 管理子任务和任务关联"""
    
    def __init__(self, store: 'TaskStore'):
        self.store = store
    
    def get_subtasks(self, parent_task_id: int, user_id: str = 'default') -> list['Task']:
        """获取父任务的所有子任务"""
        return self.store.get_subtasks(parent_task_id, user_id=user_id)
    
    def get_task_with_subtasks(self, task_id: int, user_id: str = 'default') -> Optional['Task']:
        """获取任务及其所有子任务"""
        return self.store.get_task_with_subtasks(task_id, user_id=user_id)
    
    def create_subtask(
        self,
        parent_task_id: int,
        title: str,
        *,
        due_at: datetime | None = None,
        priority: 'Priority' = None,
        estimated_minutes: int | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        user_id: str = 'default'
    ) -> 'Task':
        """创建子任务"""
        if priority is None:
            from ..models import Priority
            priority = Priority.MEDIUM
        
        return self.store.create_subtask(
            parent_task_id,
            title,
            due_at=due_at,
            priority=priority,
            estimated_minutes=estimated_minutes,
            notes=notes,
            tags=tags,
            user_id=user_id,
        )
    
    def bulk_create_subtasks(
        self,
        parent_task_id: int,
        subtasks: list[dict],
        user_id: str = 'default'
    ) -> list['Task']:
        """批量创建子任务"""
        return self.store.bulk_create_subtasks(parent_task_id, subtasks, user_id=user_id)
    
    def get_parent_task(self, task_id: int, user_id: str = 'default') -> Optional['Task']:
        """获取父任务"""
        return self.store.get_parent_task(task_id, user_id=user_id)
    
    def add_relation(
        self,
        source_task_id: int,
        target_task_id: int,
        relation_type: 'TaskRelationType',
        user_id: str = 'default'
    ) -> Optional['TaskRelation']:
        """添加任务关系"""
        return self.store.add_task_relation(source_task_id, target_task_id, relation_type, user_id=user_id)
    
    def remove_relation(
        self,
        source_task_id: int,
        target_task_id: int,
        relation_type: 'TaskRelationType',
        user_id: str = 'default'
    ) -> bool:
        """移除任务关系"""
        return self.store.remove_task_relation(source_task_id, target_task_id, relation_type, user_id=user_id)
    
    def get_task_relations(self, task_id: int, user_id: str = 'default') -> list['TaskRelation']:
        """获取任务的所有关系"""
        return self.store.get_task_relations(task_id, user_id=user_id)
    
    def add_dependency(
        self,
        task_id: int,
        depends_on_task_id: int,
        user_id: str = 'default'
    ) -> Optional['TaskRelation']:
        """添加依赖关系"""
        return self.store.add_dependency(task_id, depends_on_task_id, user_id=user_id)
    
    def remove_dependency(
        self,
        task_id: int,
        depends_on_task_id: int,
        user_id: str = 'default'
    ) -> bool:
        """移除依赖关系"""
        return self.store.remove_dependency(task_id, depends_on_task_id, user_id=user_id)
    
    def get_dependencies(self, task_id: int, user_id: str = 'default') -> list['Task']:
        """获取任务所依赖的任务"""
        return self.store.get_dependencies(task_id, user_id=user_id)
    
    def get_dependents(self, task_id: int, user_id: str = 'default') -> list['Task']:
        """获取依赖该任务的任务"""
        return self.store.get_dependents(task_id, user_id=user_id)
    
    def get_related_tasks(self, task_id: int, user_id: str = 'default') -> list['Task']:
        """获取相关任务"""
        return self.store.get_related_tasks(task_id, user_id=user_id)
    
    def is_blocked(self, task_id: int, user_id: str = 'default') -> bool:
        """检查任务是否被阻塞"""
        return self.store.is_task_blocked(task_id, user_id=user_id)
    
    def get_task_hierarchy(self, task_id: int, user_id: str = 'default') -> dict:
        """获取任务层级信息
        
        Args:
            task_id: 任务ID
            user_id: 用户ID
            
        Returns:
            包含任务、子任务、依赖关系的完整信息
        """
        task = self.store._get_task(task_id)
        if not task:
            return None
        
        parent = self.get_parent_task(task_id, user_id)
        subtasks = self.get_subtasks(task_id, user_id)
        dependencies = self.get_dependencies(task_id, user_id)
        dependents = self.get_dependents(task_id, user_id)
        related = self.get_related_tasks(task_id, user_id)
        relations = self.get_task_relations(task_id, user_id)
        
        def task_to_dict(t: 'Task') -> dict:
            return {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None,
            }
        
        def relation_to_dict(r: 'TaskRelation') -> dict:
            return {
                "id": r.id,
                "source_task_id": r.source_task_id,
                "target_task_id": r.target_task_id,
                "relation_type": r.relation_type.value,
            }
        
        return {
            "task": task_to_dict(task),
            "parent": task_to_dict(parent) if parent else None,
            "subtasks": [task_to_dict(t) for t in subtasks],
            "dependencies": [task_to_dict(t) for t in dependencies],
            "dependents": [task_to_dict(t) for t in dependents],
            "related": [task_to_dict(t) for t in related],
            "relations": [relation_to_dict(r) for r in relations],
            "is_blocked": self.is_blocked(task_id, user_id),
        }
