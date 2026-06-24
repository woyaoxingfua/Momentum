"""
任务关联工具 - Task Relation Tools
提供任务依赖、关联等工具函数
"""
import json
from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def _to_json(obj) -> str:
    """将对象转换为 JSON 字符串，确保工具输出为文本格式"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def create_relation_tools(store: 'TaskStore', user_id: str):
    """创建任务关联相关的工具函数"""
    
    @function_tool
    def add_task_dependency(task_id: int, depends_on_task_id: int) -> str:
        """添加任务依赖
        
        Args:
            task_id: 任务ID
            depends_on_task_id: 被依赖的任务ID
        """
        relation = store.add_dependency(task_id, depends_on_task_id, user_id=user_id)
        if not relation:
            return f"无法创建依赖关系，请检查任务 #{task_id} 和 #{depends_on_task_id} 是否存在"
        return f"已创建依赖关系：任务 #{task_id} 依赖任务 #{depends_on_task_id}"
    
    @function_tool
    def remove_task_dependency(task_id: int, depends_on_task_id: int) -> str:
        """移除任务依赖
        
        Args:
            task_id: 任务ID
            depends_on_task_id: 被依赖的任务ID
        """
        success = store.remove_dependency(task_id, depends_on_task_id, user_id=user_id)
        if not success:
            return f"未找到该依赖关系"
        return f"已移除依赖关系：任务 #{task_id} 不再依赖任务 #{depends_on_task_id}"
    
    @function_tool
    def get_task_dependencies(task_id: int) -> str:
        """获取任务依赖
        
        Args:
            task_id: 任务ID
        """
        dependencies = store.get_dependencies(task_id, user_id=user_id)
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None
            }
            for t in dependencies
        ])
    
    @function_tool
    def get_task_dependents(task_id: int) -> str:
        """获取依赖该任务的任务
        
        Args:
            task_id: 任务ID
        """
        dependents = store.get_dependents(task_id, user_id=user_id)
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None
            }
            for t in dependents
        ])
    
    @function_tool
    def add_task_relation(source_task_id: int, target_task_id: int, relation_type: str = "relates_to") -> str:
        """添加任务关系
        
        Args:
            source_task_id: 源任务ID
            target_task_id: 目标任务ID
            relation_type: 关系类型（depends_on, blocks, relates_to, follows, parent_of）
        """
        from ...models import TaskRelationType
        
        try:
            rel_type = TaskRelationType(relation_type)
        except ValueError:
            return f"无效的关系类型，请使用：depends_on, blocks, relates_to, follows, parent_of"
        
        relation = store.add_task_relation(source_task_id, target_task_id, rel_type, user_id=user_id)
        if not relation:
            return f"无法创建关系，请检查任务是否存在"
        return f"已创建关系：任务 #{source_task_id} {relation_type} 任务 #{target_task_id}"
    
    @function_tool
    def get_task_relations(task_id: int) -> str:
        """获取任务的所有关系
        
        Args:
            task_id: 任务ID
        """
        relations = store.get_task_relations(task_id, user_id=user_id)
        return _to_json([
            {
                "id": r.id,
                "source_task_id": r.source_task_id,
                "target_task_id": r.target_task_id,
                "relation_type": r.relation_type.value,
                "created_at": r.created_at.isoformat()
            }
            for r in relations
        ])
    
    @function_tool
    def is_task_blocked(task_id: int) -> str:
        """检查任务是否被阻塞
        
        Args:
            task_id: 任务ID
        """
        return _to_json({"blocked": store.is_task_blocked(task_id, user_id=user_id)})
    
    return [
        add_task_dependency,
        remove_task_dependency,
        get_task_dependencies,
        get_task_dependents,
        add_task_relation,
        get_task_relations,
        is_task_blocked,
    ]
