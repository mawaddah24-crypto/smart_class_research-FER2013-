# train_hcavit_fastmulti.py

import os, csv
import torch
import argparse
import torch.amp
import torch.nn as nn
import torch.optim as optim
from torch.amp import GradScaler
from timm.scheduler import CosineLRScheduler
from torch.utils.data import DataLoader
from torchvision import transforms,datasets
from tqdm import tqdm
import pandas as pd
import numpy as np
from DualPathModel import DualPath_Baseline, DualPath_Fusion, DualPath_CR_DRM
import torchvision.transforms.functional as TF

from loaders import load_pretrained_backbone
#from FERLandmarkDataset import FERLandmarkCachedDataset  # Sesuaikan ini
from FocalLoss import FocalLoss

# === Cosine Warmup Scheduler ===
class CosineWarmupScheduler(torch.optim.lr_scheduler._LRScheduler):
    def __init__(self, optimizer, warmup_epochs, max_epochs, min_lr=1e-6, last_epoch=-1):
        self.warmup_epochs = warmup_epochs
        self.max_epochs = max_epochs
        self.min_lr = min_lr
        super().__init__(optimizer, last_epoch)

    def get_lr(self):
        if self.last_epoch < self.warmup_epochs:
            return [base_lr * (self.last_epoch + 1) / self.warmup_epochs for base_lr in self.base_lrs]
        else:
            progress = (self.last_epoch - self.warmup_epochs) / (self.max_epochs - self.warmup_epochs)
            cosine_decay = 0.5 * (1 + torch.cos(torch.tensor(progress * 3.1415926535)))
            return [self.min_lr + (base_lr - self.min_lr) * cosine_decay for base_lr in self.base_lrs]
        
# --------------------------
# Fungsi Augmentasi MixUp & CutMix
# --------------------------
def mixup_data(x, y, alpha=1.0):
    """MixUp Augmentasi"""
    if alpha > 0:
        lam = np.random.beta(alpha, alpha)
    else:
        lam = 1

    batch_size = x.size(0)
    index = torch.randperm(batch_size).to(x.device)

    mixed_x = lam * x + (1 - lam) * x[index, :]
    y_a, y_b = y, y[index]
    return mixed_x, y_a, y_b, lam

def cutmix_data(x, y, alpha=1.0):
    """CutMix Augmentasi"""
    if alpha > 0:
        lam = np.random.beta(alpha, alpha)
    else:
        lam = 1

    batch_size, _, H, W = x.size()
    index = torch.randperm(batch_size).to(x.device)

    # Tentukan lokasi cutout
    rx = np.random.randint(W)
    ry = np.random.randint(H)
    rw = int(W * np.sqrt(1 - lam))
    rh = int(H * np.sqrt(1 - lam))

    x[:, :, ry:ry+rh, rx:rx+rw] = x[index, :, ry:ry+rh, rx:rx+rw]
    y_a, y_b = y, y[index]
    
    return x, y_a, y_b, lam

def set_backbone_trainable(model, trainable: bool):
    for param in model.backbone.parameters():
        param.requires_grad = trainable
    print(f"{'🔥 Unfrozen' if trainable else '🧊 Frozen'} backbone")


def train(args):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    os.makedirs(args.output_dir, exist_ok=True)
    
    
    # 🔧 Inisialisasi model
    if args.model == "baseline":
        model = DualPath_Baseline(num_classes=args.num_classes, pretrained=True)
    elif args.model == "fusion":
        model = DualPath_Fusion(num_classes=args.num_classes, pretrained=True)
    else:
        model = DualPath_CR_DRM(num_classes=args.num_classes, pretrained=True)
        
    model.to(device)
    
    checkpoint_path = os.path.join(args.output_dir, f'{args.model}_{args.dataset}_last.pt')
    base_path = os.path.join(args.output_dir, f'{args.model}_{args.dataset}_best.pt')
    log_file = os.path.join(args.output_dir, f'{args.model}_{args.dataset}_log.csv')
    
    if args.optimizer == 'adamw':
        optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    else:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, weight_decay=1e-4, momentum=0.9)
    
    #optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', patience=3, factor=0.5)
    #scheduler = CosineLRScheduler(optimizer, t_initial=args.epochs, lr_min=1e-5)
    #scheduler = CosineWarmupScheduler(optimizer, warmup_epochs=5, max_epochs=args.epochs, min_lr=1e-5)
    # 🧠 Load EfficientViT pretrained dari VGGFace2
    if args.backbone_weights:
        load_pretrained_backbone(model, args.backbone_weights)

    #criterion = nn.CrossEntropyLoss()
    criterion = FocalLoss(gamma=2.0)
    scaler = GradScaler(device='cuda')

    train_transforms1 = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(degrees=20),
        transforms.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.3, hue=0.1),
        transforms.RandomApply([transforms.GaussianBlur(kernel_size=5)], p=0.3),  # Blur setelah color jitter
        transforms.RandomErasing(p=0.3, scale=(0.02, 0.2)),  # Erasing applied after tensor
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    
    train_transforms = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),  # Konversi ke Tensor harus di awal!
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(10),
        transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        transforms.RandomErasing(p=0.2, scale=(0.02, 0.2)),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    val_transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])
    train_path = os.path.join(args.data_dir, args.dataset, 'train')
    val_path = os.path.join(args.data_dir, args.dataset, 'test')
    if len(train_path) == 0 or len(val_path) == 0:
        raise FileNotFoundError(f"❌ Path dataset tidak ditemukan: {train_dataset}")
       
    train_dataset = datasets.ImageFolder(train_path, train_transforms)
    val_dataset = datasets.ImageFolder(val_path, val_transform)
    print(f"📊 Train Dataset {args.dataset}: {len(train_dataset)} | Val samples: {len(val_dataset)}")
        
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=4,pin_memory=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=4,pin_memory=True)

    # 📈 Logging
    start_epoch = 0
    best_acc = 0
    history = []
    os.makedirs(args.output_dir, exist_ok=True)
    early_stop_counter = 0
    
    print(f"✅ Train Model: {args.model}")
    
    if os.path.exists(checkpoint_path):
        print(f"🔁 Auto-loading last checkpoint from: {checkpoint_path}")
        checkpoint = torch.load(checkpoint_path, map_location=device,weights_only=True)
        model.load_state_dict(checkpoint['model_state_dict'], strict=False)
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scaler.load_state_dict(checkpoint['scaler_state_dict'])
        best_acc = checkpoint.get('best_acc', best_acc)
        start_epoch = checkpoint.get('epoch', 0)
        if os.path.exists(log_file):
            history = pd.read_csv(log_file).to_dict('records')

    for epoch in range(start_epoch, args.epochs):
        model.train()
        
        if args.model == "crdrm":
            model.update_confidence_epoch(epoch)  # Ini penting!
            
        running_loss = 0.0
        correct = 0
        total = 0
        if epoch < 4:
            set_backbone_trainable(model, False)
        else:
            set_backbone_trainable(model, True)
        
        if args.model == "baseline":
            if epoch < 5:
                model.delay_ddga = False
                print(f"⏹️ Dual Dynamic Gated Attention Fusion Non Aktif")
            else:
                print(f"✅ Dual Dynamic Gated Attention Fusion Aktif ")
            
        loop = tqdm(train_loader, desc=f"Epoch [{epoch+1}/{args.epochs}]", unit='batch')
        for batch_idx, (imgs, labels) in enumerate(loop):
            imgs, labels = imgs.to(device), labels.to(device)
            
            if np.random.rand() < 0.3:
                if np.random.rand() < 0.5:
                    imgs, labels_a, labels_b, lam = mixup_data(imgs, labels, alpha=0.2)
                else:
                    imgs, labels_a, labels_b, lam = cutmix_data(imgs, labels, alpha=0.3)
            else:
                labels_a = labels_b = labels
                lam = 1.0
                
            optimizer.zero_grad()
            with torch.amp.autocast(device_type='cuda'):
                #outputs = model(imgs)
                outputs = model(imgs)
                loss = lam * criterion(outputs, labels_a) + (1 - lam) * criterion(outputs, labels_b) 
                
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            
            running_loss += loss.item()
            _, predicted = torch.max(outputs, 1)
            correct += (predicted == labels).sum().item()
            total += labels.size(0)
            loop.set_postfix(loss=loss.item(), acc=100.*correct/total)
            
        train_acc = 100. * correct / total
        # 🔍 Validation
        model.eval()
        val_correct = 0
        val_total = 0
        val_loss = 0.0
        
        val_loader_tqdm = tqdm(val_loader, desc=f"Epoch {epoch+1}/{args.epochs} [Validation]", unit="batch")
        with torch.no_grad():
            for batch_idx, (imgs, labels) in enumerate(val_loader_tqdm):
                imgs, labels = imgs.to(device), labels.to(device)
                with torch.amp.autocast(device_type='cuda'):
                    outputs = model(imgs)
                    loss = criterion(outputs, labels)

                val_loss += loss.item()
                _, predicted = torch.max(outputs, 1)
                val_correct += (predicted == labels).sum().item()
                
                val_total += labels.size(0)
                val_loader_tqdm.set_postfix(val_loss=f"{loss.item():.4f}", acc=100.*val_correct/val_total)
                
        val_acc = 100 * val_correct / val_total
        val_loss_avg = val_loss / len(val_loader)
        scheduler.step(val_loss)
        #scheduler.step()
        print(f"\nEpoch {epoch+1}: Train Acc: {train_acc:.2f}% | Val Acc: {val_acc:.2f}% | Val Loss: {val_loss_avg:.4f}")
        
        torch.save({
            'epoch': epoch + 1,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'scaler_state_dict': scaler.state_dict(),
            'best_acc': best_acc
        }, checkpoint_path)
        
        # 💾 Save best model
        if val_acc > best_acc:
            best_acc = val_acc
            torch.save(model.state_dict(), base_path)
            print(f"✅ Model Terbaik di Simpan Acc: {best_acc:.2f}%")
            early_stop_counter = 0
        else:
            early_stop_counter += 1
            print(f"⏹️ Early stopping {early_stop_counter} dari {args.early_stop}")
            if early_stop_counter >= args.early_stop:
                print("⏹️ Early stopping triggered.")
                break
            
        # ⏺️ Logging CSV
        row = {"epoch": epoch+1, "train_loss": running_loss / len(train_loader),
       "val_loss": val_loss_avg, "val_loss_acc": val_loss,"val_acc": val_acc}
        history.append(row)
        pd.DataFrame(history).to_csv(log_file, index=False)
        
        torch.save({
            'epoch': epoch + 1,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'scaler_state_dict': scaler.state_dict(),
            'best_acc': best_acc
        }, checkpoint_path)
        

# ⛳ Entry Point
if __name__ == "__main__":
    torch.cuda.empty_cache()
    torch.backends.cudnn.benchmark = True
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='RAF-DB')
    parser.add_argument('--data_dir', type=str, default='../dataset/')
    parser.add_argument("--output_dir", type=str, default="logs/")
    parser.add_argument("--backbone_weights", type=str, default="./weights/efficientvit_vggface2_best.pth")
    parser.add_argument("--num_classes", type=int, default=7)
    parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=150)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--early_stop", type=int, default=10)
    parser.add_argument('--model', type=str, default='baseline', choices=['baseline','fusion','crdrm'])
    parser.add_argument('--optimizer', type=str, default='adamw', choices=['adamw', 'sgd'])
    parser.add_argument('--ddga', action='store_true', help="Dynamic attention to fuse both pathways")
    args = parser.parse_args()
    print(f"✅ Konfigurasi : {args}")
    train(args)
