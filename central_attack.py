import argparse

import torch.nn.functional as F
from utils.data_provider import *
from utils.hamming_matching import *
from model.attack_model.util import load_model, get_database_code, generate_code
from utils.util import Logger


def adv_loss(noisy_output, target_hash):
    loss = torch.mean(noisy_output * target_hash)
    # sim = noisy_output * target_hash
    # w = (sim>-0.5).int()
    # sim = w*(sim+2)*sim
    # loss = torch.mean(sim)
    return loss


def hash_adv(model, query, target_hash, epsilon, step=1, iteration=2000, randomize=False):
    delta = torch.zeros_like(query).cuda()
    if randomize:
        delta.uniform_(-epsilon, epsilon)
        delta.data = (query.data + delta.data).clamp(0, 1) - query.data
    delta.requires_grad = True

    for i in range(iteration):
        noisy_output = model(query + delta)
        loss = adv_loss(noisy_output, target_hash.detach())
        loss.backward()

        # delta.data = delta - step * delta.grad.detach() / (torch.norm(delta.grad.detach(), 2) + 1e-9)
        delta.data = delta - step / 255 * torch.sign(delta.grad.detach())
        # delta.data = delta - step * delta.grad.detach()
        delta.data = delta.data.clamp(-epsilon, epsilon)
        delta.data = (query.data + delta.data).clamp(0, 1) - query.data
        delta.grad.zero_()

        # if i % 10 == 0:
        #     print('it:{}, loss:{}'.format(i, loss))
    # print(torch.min(255*delta.data))
    # print(torch.max(255*delta.data))
    return query + delta.detach()


def hash_center_code(y, B, L, bit):
    code = torch.zeros(y.size(0), bit).cuda()
    for i in range(y.size(0)):
        l = y[i].repeat(L.size(0), 1)
        w = torch.sum(l * L, dim=1) / torch.sum(torch.sign(l + L), dim=1)
        w1 = w.repeat(bit, 1).t()
        w2 = 1 - torch.sign(w1)
        code[i] = torch.sign(torch.sum(w1 * B - w2 * B, dim=0))
    return code


def sample_image(image, name, sample_dir='sample/attack'):
    image = image.cpu().detach()[2]
    image = transforms.ToPILImage()(image.float())
    image.save(os.path.join(sample_dir, name + '.png'), quality=100)


def central_attack(args, epsilon=0.039):
    os.environ["CUDA_VISIBLE_DEVICES"] = args.device
    method = 'CentralAttack'
    # load model
    attack_model = '{}_{}_{}_{}'.format(args.dataset, args.hash_method, args.backbone, args.bit)
    model_path = 'checkpoint/{}.pth'.format(attack_model)
    model = load_model(model_path)

    # load dataset
    database_loader, num_database = get_data_loader(args.data_dir, args.dataset, 'database',
                                                    args.batch_size, shuffle=False)
    train_loader, num_train = get_data_loader(args.data_dir, args.dataset, 'test',
                                              args.batch_size, shuffle=True)
    test_loader, num_test = get_data_loader(args.data_dir, args.dataset, 'test',
                                            args.batch_size, shuffle=False)

    # load hashcode and labels
    database_hash, _ = get_database_code(model, database_loader, attack_model)
    test_labels = get_data_label(args.data_dir, args.dataset, 'test')
    database_labels = get_data_label(args.data_dir, args.dataset, 'database')

    #
    train_B, train_L = generate_code(model, train_loader)
    train_B, train_L = torch.from_numpy(train_B), torch.from_numpy(train_L)
    train_B, train_L = train_B.cuda(), train_L.cuda()

    qB = np.zeros([num_test, args.bit], dtype=np.float32)
    cB = np.zeros([num_test, args.bit], dtype=np.float32)
    perceptibility = 0
    for it, data in enumerate(test_loader):
        queries, labels, index = data
        queries = queries.cuda()
        labels = labels.cuda()
        batch_size_ = index.size(0)

        n = index[-1].item() + 1
        print(n)

        center_codes = hash_center_code(labels, train_B, train_L, args.bit)
        query_adv = hash_adv(model, queries, center_codes, epsilon, iteration=args.iteration)

        perceptibility += F.mse_loss(queries, query_adv).data * batch_size_

        query_code = model(query_adv)
        query_code = torch.sign(query_code)
        qB[index.numpy(), :] = query_code.cpu().data.numpy()
        cB[index.numpy(), :] = (-center_codes).cpu().data.numpy()

        # sample_image(queries, '{}_benign'.format(it))
        # sample_image(query_adv, '{}_adv'.format(it))

    logger = Logger(os.path.join('log', attack_model), '{}.txt'.format(method))
    logger.log('perceptibility: {:.7f}'.format(torch.sqrt(perceptibility/num_test)))

    map_val = cal_map(database_hash, cB, database_labels, test_labels, 5000)
    logger.log('Theory MAP(retrieval database): {}'.format(map_val))
    map_val = cal_map(database_hash, qB, database_labels, test_labels, 5000)
    logger.log('Central Attack MAP(retrieval database): {}'.format(map_val))


def parser_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--method', dest='method', default='hag', help='name of attack method')
    parser.add_argument('--dataset_name', dest='dataset', default='NUS-WIDE',
                        choices=['CIFAR-10', 'ImageNet', 'FLICKR-25K', 'NUS-WIDE', 'MS-COCO'],
                        help='name of the dataset')
    parser.add_argument('--data_dir', dest='data_dir', default='../data/', help='path of the dataset')
    parser.add_argument('--device', dest='device', type=str, default='0', help='gpu device')
    parser.add_argument('--hash_method', dest='hash_method', default='DPH',
                        choices=['DPH', 'DPSH', 'HashNet'],
                        help='deep hashing methods')
    parser.add_argument('--backbone', dest='backbone', default='AlexNet',
                        choices=['AlexNet', 'VGG11', 'VGG16', 'VGG19', 'ResNet18', 'ResNet50'],
                        help='backbone network')
    parser.add_argument('--code_length', dest='bit', type=int, default=32, help='length of the hashing code')
    parser.add_argument('--batch_size', dest='batch_size', type=int, default=32, help='number of images in one batch')
    parser.add_argument('--iteration', dest='iteration', type=int, default=100, help='number of images in one batch')
    return parser.parse_args()


if __name__ == '__main__':
    central_attack(parser_arguments())
