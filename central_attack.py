import argparse

import torch.nn.functional as F
from utils.data_provider import *
from utils.hamming_matching import *
from model.attack_model.util import load_model, get_database_code, generate_code, get_alpha
from utils.util import Logger
from tqdm import tqdm


def adv_loss(noisy_output, target_hash):
    # loss = torch.mean(noisy_output * target_hash)
    sim = noisy_output * target_hash
    w = (sim > -0.5).int()
    sim = w * (sim + 2) * sim
    loss = torch.mean(sim)
    return loss


def hash_adv(model, query, target_hash, epsilon, step=1.0, iteration=100, randomize=False):
    delta = torch.zeros_like(query).cuda()
    if randomize:
        delta.uniform_(-epsilon, epsilon)
        delta.data = (query.data + delta.data).clamp(0, 1) - query.data
    delta.requires_grad = True

    # loss_list = []
    for i in range(iteration):
        # alpha = get_alpha(i, iteration)
        alpha = 0.1
        noisy_output = model(query + delta, alpha)
        loss = adv_loss(noisy_output, target_hash.detach())
        loss.backward()

        # delta.data = delta - step * delta.grad.detach() / (torch.norm(delta.grad.detach(), 2) + 1e-9)
        delta.data = delta - step / 255 * torch.sign(delta.grad.detach())
        # delta.data = delta - step * delta.grad.detach()
        delta.data = delta.data.clamp(-epsilon, epsilon)
        delta.data = (query.data + delta.data).clamp(0, 1) - query.data
        delta.grad.zero_()

    #     if (i + 1) % (iteration // 10) == 0:
    #         loss_list.append(round(loss.item(), 4))
    # print("loss :{}".format(loss_list))
    return query + delta.detach()


def hash_center_code(y, B, L, bit):
    code = torch.zeros(y.size(0), bit).cuda()
    for i in range(y.size(0)):
        l = y[i].repeat(L.size(0), 1)
        w = torch.sum(l * L, dim=1) / torch.sum(torch.sign(l + L), dim=1)
        w1 = w.repeat(bit, 1).t()
        w2 = 1 - torch.sign(w1)
        c = w2.sum() / bit
        w1 = 1 - w2
        # code[i] = torch.sign(torch.sum(w1*B-w2*B, dim=0))
        code[i] = torch.sign(torch.sum(c * w1 * B - (L.size(0) - c) * w2 * B, dim=0))
        # code[i] = torch.sign(torch.sum(w1*B, dim=0))
    return code


def sample_image(image, name, sample_dir='sample/attack'):
    image = image.cpu().detach()[2]
    image = transforms.ToPILImage()(image.float())
    image.save(os.path.join(sample_dir, name + '.png'), quality=100)


def central_attack(args, epsilon=8/255.):
    os.environ["CUDA_VISIBLE_DEVICES"] = args.device
    method = 'CentralAttack'
    # load model
    attack_model = '{}_{}_{}_{}'.format(args.dataset, args.hash_method, args.backbone, args.bit)
    model_path = 'checkpoint/{}.pth'.format(attack_model)
    model = load_model(model_path)

    # load dataset
    database_loader, num_database = get_data_loader(args.data_dir, args.dataset, 'database',
                                                    args.batch_size, shuffle=False)
    train_loader, num_train = get_data_loader(args.data_dir, args.dataset, 'train',
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
    qB_ori = np.zeros([num_test, args.bit], dtype=np.float32)
    cB = np.zeros([num_test, args.bit], dtype=np.float32)
    perceptibility = 0
    for it, data in enumerate(tqdm(test_loader, ncols=50)):
        queries, labels, index = data
        queries = queries.cuda()
        labels = labels.cuda()
        batch_size_ = index.size(0)

        center_codes = hash_center_code(labels, train_B, train_L, args.bit)
        query_adv = hash_adv(model, queries, center_codes, epsilon, iteration=args.iteration)

        perceptibility += F.mse_loss(queries, query_adv).data * batch_size_
        query_code = model(query_adv)
        query_code = torch.sign(query_code)
        qB[index.numpy(), :] = query_code.cpu().data.numpy()
        qB_ori[index.numpy(), :] = model(queries).sign().cpu().data.numpy()
        cB[index.numpy(), :] = center_codes.cpu().data.numpy()

        # sample_image(queries, '{}_benign'.format(it))
        # sample_image(query_adv, '{}_adv'.format(it))

    # save code
    np.save(os.path.join('log', attack_model, 'Original_code.npy'), qB_ori)
    np.save(os.path.join('log', attack_model, '{}_code.npy'.format(method)), qB)

    # calculate map
    logger = Logger(os.path.join('log', attack_model), '{}.txt'.format(method))
    logger.log('perceptibility: {:.7f}'.format(torch.sqrt(perceptibility / num_test)))

    map_val = cal_map(database_hash, qB, database_labels, test_labels, 5000)
    logger.log('Central Attack MAP(retrieval database): {}'.format(map_val))
    map_val = cal_map(database_hash, -cB, database_labels, test_labels, 5000)
    logger.log('Theory MAP(retrieval database): {}'.format(map_val))
    map_val = cal_map(database_hash, qB_ori, database_labels, test_labels, 5000)
    logger.log('Ori MAP(retrieval database): {}'.format(map_val))

    # # calculate P-R curve
    # pr_arr = cal_pr(database_hash, qB_ori, database_labels, test_labels, interval=0.01)
    # np.save(os.path.join('log', attack_model, '{}-pr_ori.npy'.format(method)), pr_arr)
    #
    # pr_arr = cal_pr(database_hash, qB, database_labels, test_labels, interval=0.01)
    # np.save(os.path.join('log', attack_model, '{}-pr_adv.npy'.format(method)), pr_arr)
    #
    # top_n = cal_top_n(database_hash, qB_ori, database_labels, test_labels)
    # np.save(os.path.join('log', attack_model, '{}-topn_ori.npy'.format(method)), top_n)
    # top_n = cal_top_n(database_hash, qB, database_labels, test_labels)
    # np.save(os.path.join('log', attack_model, '{}-topn_adv.npy'.format(method)), top_n)


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
                        choices=['AlexNet', 'VGG11', 'VGG16', 'VGG19', 'ResNet18', 'ResNet50', 'ResNet101'],
                        help='backbone network')
    parser.add_argument('--code_length', dest='bit', type=int, default=32, help='length of the hashing code')
    parser.add_argument('--batch_size', dest='batch_size', type=int, default=32, help='number of images in one batch')
    parser.add_argument('--iteration', dest='iteration', type=int, default=100, help='number of images in one batch')
    return parser.parse_args()


if __name__ == '__main__':
    central_attack(parser_arguments())
