import { useState } from 'react';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Plus, Database, Filter, Wand2, Link2, Target } from 'lucide-react';
import { cn } from '@/lib/utils';

const NodeIcons = {
  source: Database,
  filter: Filter,
  transform: Wand2,
  match: Link2,
  action: Target,
};

const NodeColors = {
  source: {
    bg: 'bg-blue-50 dark:bg-blue-950/50',
    border: 'border-blue-500 dark:border-blue-600',
    text: 'text-blue-900 dark:text-blue-100',
    hover: 'hover:bg-blue-100 dark:hover:bg-blue-900/50',
  },
  filter: {
    bg: 'bg-yellow-50 dark:bg-yellow-950/50',
    border: 'border-yellow-500 dark:border-yellow-600',
    text: 'text-yellow-900 dark:text-yellow-100',
    hover: 'hover:bg-yellow-100 dark:hover:bg-yellow-900/50',
  },
  transform: {
    bg: 'bg-purple-50 dark:bg-purple-950/50',
    border: 'border-purple-500 dark:border-purple-600',
    text: 'text-purple-900 dark:text-purple-100',
    hover: 'hover:bg-purple-100 dark:hover:bg-purple-900/50',
  },
  match: {
    bg: 'bg-green-50 dark:bg-green-950/50',
    border: 'border-green-500 dark:border-green-600',
    text: 'text-green-900 dark:text-green-100',
    hover: 'hover:bg-green-100 dark:hover:bg-green-900/50',
  },
  action: {
    bg: 'bg-red-50 dark:bg-red-950/50',
    border: 'border-red-500 dark:border-red-600',
    text: 'text-red-900 dark:text-red-100',
    hover: 'hover:bg-red-100 dark:hover:bg-red-900/50',
  },
};

const PipelineNode = ({ node, isSelected, onSelect, onDoubleClick, onAddNode, isFirst, isLast }) => {
  const [showAddMenu, setShowAddMenu] = useState({ top: false, bottom: false });
  const Icon = NodeIcons[node.type] || Database;
  const colors = NodeColors[node.type] || NodeColors.source;

  const handleAddNode = (position, type) => {
    onAddNode(node.id, position, type);
    setShowAddMenu({ top: false, bottom: false });
  };

  return (
    <div className="relative">
      {/* Top Add Button */}
      {!isFirst && (
        <div className="flex justify-center mb-2 opacity-0 group-hover:opacity-100 transition-opacity">
          <DropdownMenu open={showAddMenu.top} onOpenChange={(open) => setShowAddMenu({ ...showAddMenu, top: open })}>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon"
                variant="outline"
                className="h-8 w-8 rounded-full shadow-md"
              >
                <Plus className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="center">
              <DropdownMenuItem onClick={() => handleAddNode('before', 'source')}>
                <Database className="h-4 w-4 mr-2" />
                Source
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => handleAddNode('before', 'filter')}>
                <Filter className="h-4 w-4 mr-2" />
                Filter
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => handleAddNode('before', 'transform')}>
                <Wand2 className="h-4 w-4 mr-2" />
                Transform
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => handleAddNode('before', 'match')}>
                <Link2 className="h-4 w-4 mr-2" />
                Match
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => handleAddNode('before', 'action')}>
                <Target className="h-4 w-4 mr-2" />
                Action
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}

      {/* Node Card */}
      <Card
        className={cn(
          'px-4 py-3 cursor-pointer transition-all border-2 group',
          colors.bg,
          colors.border,
          colors.hover,
          isSelected && 'ring-2 ring-primary ring-offset-2'
        )}
        onClick={() => onSelect(node)}
        onDoubleClick={() => onDoubleClick(node)}
      >
        <div className="flex items-center justify-between gap-2 mb-1">
          <div className="flex items-center gap-2">
            <Icon className={cn('h-4 w-4', colors.text)} />
            <div className={cn('font-bold text-sm', colors.text)}>
              {node.type.charAt(0).toUpperCase() + node.type.slice(1)}
            </div>
          </div>
          {node.data.configured && (
            <Badge variant="outline" className="text-xs px-1 py-0">
              ✓
            </Badge>
          )}
        </div>
        <div className={cn('text-xs opacity-80', colors.text)}>
          {node.data.summary || 'Not configured'}
        </div>
      </Card>

      {/* Bottom Add Button */}
      <div className="flex justify-center mt-2 opacity-0 group-hover:opacity-100 transition-opacity">
        <DropdownMenu open={showAddMenu.bottom} onOpenChange={(open) => setShowAddMenu({ ...showAddMenu, bottom: open })}>
          <DropdownMenuTrigger asChild>
            <Button
              size="icon"
              variant="outline"
              className="h-8 w-8 rounded-full shadow-md"
            >
              <Plus className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            <DropdownMenuItem onClick={() => handleAddNode('after', 'source')}>
              <Database className="h-4 w-4 mr-2" />
              Source
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => handleAddNode('after', 'filter')}>
              <Filter className="h-4 w-4 mr-2" />
              Filter
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => handleAddNode('after', 'transform')}>
              <Wand2 className="h-4 w-4 mr-2" />
              Transform
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => handleAddNode('after', 'match')}>
              <Link2 className="h-4 w-4 mr-2" />
              Match
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => handleAddNode('after', 'action')}>
              <Target className="h-4 w-4 mr-2" />
              Action
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Connector Line */}
      {!isLast && (
        <div className="flex justify-center my-2">
          <div className="w-0.5 h-4 bg-blue-500 dark:bg-green-500" />
        </div>
      )}
    </div>
  );
};

const PipelineColumn = ({ nodes, selectedNode, onNodeSelect, onNodeDoubleClick, onAddNode, pipelineId }) => {
  return (
    <div className="flex flex-col items-center w-full max-w-md mx-auto p-4">
      <div className="w-full space-y-0 group">
        {nodes.map((node, index) => (
          <PipelineNode
            key={node.id}
            node={node}
            isSelected={selectedNode?.id === node.id}
            onSelect={onNodeSelect}
            onDoubleClick={onNodeDoubleClick}
            onAddNode={onAddNode}
            isFirst={index === 0}
            isLast={index === nodes.length - 1}
          />
        ))}
      </div>
    </div>
  );
};

export default PipelineColumn;
